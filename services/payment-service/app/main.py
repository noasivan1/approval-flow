import asyncio
import contextlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from pydantic import ValidationError

from . import events
from .config import settings
from .logging_config import configure_logging, set_correlation_id
from .models import PaymentRequest
from .provider import get_provider
from .repository import get_repository, money

configure_logging(settings.log_level)
logger = logging.getLogger(settings.service_name)

provider = get_provider()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _precheck_failure(request: PaymentRequest) -> str | None:
    if request.amount_usd is None:
        return "missing_usd_amount"
    if not (request.department or "").strip():
        return "missing_department"
    return None


def _new_payment_record(request: PaymentRequest, now: str) -> dict:
    return {
        # derived from tracking_id, so a redelivered request builds the same record
        "payment_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"approvalflow/payment/{request.tracking_id}")),
        "tracking_id": request.tracking_id,
        "department": request.department,
        "amount_usd": money(request.amount_usd) if request.amount_usd is not None else money(0),
        "currency": request.currency or "USD",
        "invoice_number": request.invoice_number,
        "status": "reserved",
        "failure_reason": None,
        "correlation_id": request.correlation_id,
        "requested_at": request.requested_at or now,
        "executed_at": None,
        "released_at": None,
        "result_published_at": None,
        "created_at": now,
        "updated_at": now,
    }


def _execute_reserved(payment: dict) -> dict:
    """Run the provider for a reserved payment and finalize it; the guarded
    status transitions make a repeated call a no-op."""
    repository = get_repository()
    ok, reason = provider.execute(payment["payment_id"], payment.get("invoice_number"), payment["amount_usd"])
    now = _utc_now()
    if ok:
        repository.commit_paid(payment["payment_id"], now)
    else:
        repository.fail_reserved(payment["payment_id"], reason, now)
    return repository.get_payment(payment["tracking_id"])


def _result_event(payment: dict) -> dict:
    return {
        "event_id": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"approvalflow/payment-result/{payment['tracking_id']}")
        ),
        "tracking_id": payment["tracking_id"],
        "correlation_id": payment["correlation_id"],
        "payment_id": payment["payment_id"],
        "status": "paid" if payment["status"] == "paid" else "payment_failed",
        "failure_reason": payment.get("failure_reason"),
        "reservation_released": payment.get("released_at") is not None,
        "amount_usd": float(payment["amount_usd"]),
        "requested_at": payment.get("requested_at"),
        "executed_at": payment.get("executed_at"),
        "released_at": payment.get("released_at"),
    }


async def _publish_result(payment: dict) -> None:
    await events.publish(events.TOPIC_PAYMENT_RESULT, _result_event(payment))
    get_repository().mark_result_published(payment["payment_id"], _utc_now())


async def recover_pending() -> int:
    """Resume reserved payments and republish unsent results."""
    repository = get_repository()
    recovered = 0
    for payment in repository.find_reserved():
        payment = _execute_reserved(payment)
        await _publish_result(payment)
        recovered += 1
    for payment in repository.find_unpublished_results():
        await _publish_result(payment)
        recovered += 1
    return recovered


async def _recovery_loop() -> None:
    interval = float(os.getenv("PAYMENT_RETRY_SECONDS", "30"))
    while True:
        await asyncio.sleep(interval)
        try:
            count = await recover_pending()
            if count:
                logger.info("payment recovery handled %d payment(s)", count)
        except Exception:
            logger.exception("payment recovery sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    recovery = asyncio.create_task(_recovery_loop())
    yield
    recovery.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await recovery


app = FastAPI(title=settings.service_name, lifespan=lifespan)


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
            "topic": events.TOPIC_PAYMENT_REQUESTED,
            "route": "/events/payment-requested",
        }
    ]


@app.post("/events/payment-requested")
async def handle_payment_requested(event: dict):
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict) or not data.get("event_id"):
        logger.error("dropping payment.requested event without a data payload")
        return events.EVENT_DROP
    try:
        request = PaymentRequest(**{key: data.get(key) for key in PaymentRequest.model_fields})
    except ValidationError:
        logger.exception("dropping malformed payment.requested event")
        return events.EVENT_DROP

    set_correlation_id(request.correlation_id)
    try:
        repository = get_repository()
        now = _utc_now()
        outcome, payment = repository.create_payment(
            _new_payment_record(request, now), _precheck_failure(request)
        )
        if payment["status"] == "reserved":
            # first attempt, or a redelivery that found a reserved payment: resume
            payment = _execute_reserved(payment)
        await _publish_result(payment)
    except Exception:
        logger.exception("transient failure processing payment for %s", request.tracking_id)
        return events.EVENT_RETRY

    logger.info(
        "payment %s for %s -> %s%s",
        payment["payment_id"], request.tracking_id, payment["status"],
        f" ({payment['failure_reason']})" if payment.get("failure_reason") else "",
    )
    return events.EVENT_SUCCESS
