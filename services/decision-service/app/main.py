import logging
import uuid
from collections import deque
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from pydantic import ValidationError

from . import events
from .config import settings
from .logging_config import configure_logging, set_correlation_id
from .models import InvoiceData
from .policy import load_policy
from .recommendation import get_provider
from .router import evaluate

configure_logging(settings.log_level)
logger = logging.getLogger(settings.service_name)

app = FastAPI(title=settings.service_name)

provider = get_provider()

# duplicate-delivery shortcut only; correctness comes from the deterministic
# event_id and the workflow-service's persisted decision
_seen_ids: set[str] = set()
_seen_order: deque[str] = deque(maxlen=1024)


def _mark_seen(event_id: str) -> None:
    if event_id in _seen_ids:
        return
    if len(_seen_order) == _seen_order.maxlen:
        _seen_ids.discard(_seen_order[0])
    _seen_order.append(event_id)
    _seen_ids.add(event_id)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    set_correlation_id(correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


@app.get("/health")
async def health():
    return {"service": settings.service_name, "status": "ok"}


@app.get("/dapr/subscribe")
def dapr_subscribe():
    return [
        {
            "pubsubname": events.PUBSUB_NAME,
            "topic": events.TOPIC_INVOICE_RECEIVED,
            "route": "/events/invoice-received",
        }
    ]


@app.post("/events/invoice-received")
async def handle_invoice_received(event: dict):
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        logger.error("dropping invoice.received event without a data payload")
        return events.EVENT_DROP

    event_id = data.get("event_id")
    tracking_id = data.get("tracking_id")
    try:
        invoice = InvoiceData(**data["invoice"])
    except (KeyError, TypeError, ValidationError):
        logger.exception("dropping malformed invoice.received event (tracking_id=%s)", tracking_id)
        return events.EVENT_DROP
    if not event_id or not tracking_id:
        logger.error("dropping invoice.received event without event_id/tracking_id")
        return events.EVENT_DROP

    set_correlation_id(data.get("correlation_id", "-"))

    if event_id in _seen_ids:
        return events.EVENT_SUCCESS

    try:
        policy = load_policy()
        recommendation = provider.recommend(invoice)
        decision = evaluate(invoice, policy, recommendation)
        decided_event = {
            # derived from the received event_id so re-deliveries produce an
            # identical decided event
            "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"approvalflow/decided/{event_id}")),
            "tracking_id": tracking_id,
            "correlation_id": data.get("correlation_id", "-"),
            "recommendation": recommendation.suggestion,
            "confidence": recommendation.confidence,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            **decision,
        }
        await events.publish(events.TOPIC_INVOICE_DECIDED, decided_event)
    except Exception:
        logger.exception("transient failure evaluating invoice %s, requesting retry", tracking_id)
        return events.EVENT_RETRY

    _mark_seen(event_id)
    logger.info("invoice %s routed to %s", tracking_id, decided_event["route"])
    return events.EVENT_SUCCESS
