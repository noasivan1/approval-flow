import asyncio
import contextlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from . import events
from .config import settings
from .logging_config import configure_logging, correlation_id_var, set_correlation_id
from .models import AdditionalInfo, InvoiceSubmission, ReviewActionRequest
from .repository import get_repository

configure_logging(settings.log_level)
logger = logging.getLogger(settings.service_name)

_DECIDED_STATUSES = {"approved", "pending_review", "rejected"}
_STATUS_BY_ACTION = {
    "approve": "approved",
    "reject": "rejected",
    "request_more_info": "needs_more_info",
}
_SWEEP_GRACE_SECONDS = 10


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _received_event(invoice: dict) -> dict:
    submission_fields = InvoiceSubmission.model_fields.keys()
    revision = invoice.get("revision", 1)
    return {
        # derived from tracking_id + revision so a sweep re-publish is
        # byte-identical while each re-evaluation round gets a new identity
        "event_id": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"approvalflow/received/{invoice['tracking_id']}/{revision}")
        ),
        "tracking_id": invoice["tracking_id"],
        "correlation_id": invoice.get("correlation_id", "-"),
        "revision": revision,
        "submitted_at": invoice["created_at"],
        "invoice": {field: invoice.get(field) for field in submission_fields},
    }


async def _publish_received(invoice: dict) -> bool:
    try:
        await events.publish(events.TOPIC_INVOICE_RECEIVED, _received_event(invoice))
    except Exception:
        logger.exception(
            "publish of invoice.received failed for %s; retry sweep will pick it up",
            invoice["tracking_id"],
        )
        return False
    get_repository().mark_published(invoice["tracking_id"], _utc_now())
    return True


def _payment_amount_usd(invoice: dict):
    decision = invoice.get("decision") or {}
    if decision.get("amount_usd") is not None:
        return decision["amount_usd"]
    if (invoice.get("currency") or "USD").upper() == "USD":
        return invoice["amount"]
    return None  # payment-service fails this safely as missing_usd_amount


def _payment_requested_event(invoice: dict) -> dict:
    return {
        "event_id": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"approvalflow/payment-requested/{invoice['tracking_id']}")
        ),
        "tracking_id": invoice["tracking_id"],
        "correlation_id": invoice.get("correlation_id", "-"),
        "amount_usd": _payment_amount_usd(invoice),
        "currency": invoice["currency"],
        "amount": invoice["amount"],
        "department": invoice.get("department"),
        "vendor": invoice["vendor"],
        "invoice_number": invoice.get("invoice_number"),
        "requested_at": invoice.get("payment_requested_at"),
    }


async def _publish_payment_requested(invoice: dict) -> bool:
    try:
        await events.publish(events.TOPIC_PAYMENT_REQUESTED, _payment_requested_event(invoice))
    except Exception:
        logger.exception(
            "publish of payment.requested failed for %s; retry sweep will pick it up",
            invoice["tracking_id"],
        )
        return False
    get_repository().mark_payment_published(invoice["tracking_id"], _utc_now())
    return True


def _start_payment(tracking_id: str, background_tasks: BackgroundTasks) -> None:
    """Shared entry point for both approval paths; a no-op unless the invoice
    is approved under the current payment flow."""
    repository = get_repository()
    if repository.start_payment(tracking_id, _utc_now()):
        logger.info("payment requested for %s", tracking_id)
        background_tasks.add_task(_publish_payment_requested, repository.get(tracking_id))


async def republish_pending() -> int:
    """Re-publish events that never went out and resume stuck payment starts.

    Best-effort recovery, not a transactional outbox: a crash between the DB
    write and the publish marker only delays the event until the next sweep.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_SWEEP_GRACE_SECONDS)).isoformat()
    repository = get_repository()
    handled = 0
    for invoice in repository.find_unpublished(cutoff):
        if await _publish_received(invoice):
            handled += 1
    for invoice in repository.find_approved_for_payment(cutoff):
        if repository.start_payment(invoice["tracking_id"], _utc_now()):
            if await _publish_payment_requested(repository.get(invoice["tracking_id"])):
                handled += 1
    for invoice in repository.find_payment_unpublished(cutoff):
        if await _publish_payment_requested(invoice):
            handled += 1
    return handled


async def _republish_loop() -> None:
    interval = float(os.getenv("PUBLISH_RETRY_SECONDS", "30"))
    while True:
        await asyncio.sleep(interval)
        try:
            count = await republish_pending()
            if count:
                logger.info("retry sweep republished %d invoice event(s)", count)
        except Exception:
            logger.exception("retry sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    sweep = asyncio.create_task(_republish_loop())
    yield
    sweep.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweep


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


def _public_view(invoice: dict) -> dict:
    hidden = ("duplicate_key", "published_at", "payment_published_at", "payment_flow_version")
    view = {key: value for key, value in invoice.items() if key not in hidden}
    if not view.get("duplicate_of"):
        view.pop("duplicate_of", None)
    return view


@app.post("/invoices", status_code=201)
def submit_invoice(submission: InvoiceSubmission, background_tasks: BackgroundTasks):
    now = _utc_now()
    invoice = {
        "tracking_id": str(uuid.uuid4()),
        "status": "received",
        "revision": 1,
        **submission.model_dump(),
        "correlation_id": correlation_id_var.get(),
        "created_at": now,
        "updated_at": now,
    }
    outcome, record = get_repository().create_invoice(invoice)
    if outcome == "duplicate":
        # no event, no decision run, no review item for duplicates
        logger.info(
            "duplicate invoice: tracking_id=%s duplicate_of=%s",
            record["tracking_id"], record["duplicate_of"],
        )
        return _public_view(record)
    # publish after the response so submission never waits on the sidecar
    background_tasks.add_task(_publish_received, record)
    logger.info("invoice received: tracking_id=%s vendor=%s", record["tracking_id"], record["vendor"])
    return _public_view(record)


@app.get("/invoices/{tracking_id}")
def get_invoice(tracking_id: str):
    repository = get_repository()
    invoice = repository.get(tracking_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    invoice["review_actions"] = repository.list_review_actions(tracking_id)
    return _public_view(invoice)


@app.get("/approvals/queue")
def approvals_queue():
    items = []
    for invoice in get_repository().list_pending_review():
        decision = invoice.get("decision") or {}
        items.append({
            "tracking_id": invoice["tracking_id"],
            "vendor": invoice["vendor"],
            "amount": invoice["amount"],
            "currency": invoice["currency"],
            "category": invoice["category"],
            "recommendation": decision.get("recommendation"),
            "confidence": decision.get("confidence"),
            "rule_ids": decision.get("rule_ids", []),
            "plain_language_reason": decision.get("plain_language_reason"),
            "review_requested_at": invoice.get("review_requested_at"),
            "revision": invoice.get("revision", 1),
        })
    return items


@app.post("/approvals/{tracking_id}/actions")
def review_action(tracking_id: str, request: ReviewActionRequest, background_tasks: BackgroundTasks):
    repository = get_repository()
    record = {
        "action_id": str(uuid.uuid4()),
        "tracking_id": tracking_id,
        "action": request.action,
        "reviewer_email": request.reviewer_email,
        "comment": request.comment,
        "idempotency_key": request.idempotency_key,
        "correlation_id": correlation_id_var.get(),
        "created_at": _utc_now(),
    }
    target_status = _STATUS_BY_ACTION[request.action]
    outcome = repository.apply_review_action(record, target_status, _utc_now())
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Invoice not found")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="Invoice is no longer awaiting review")
    if outcome == "applied":
        logger.info("review action %s on %s -> %s", request.action, tracking_id, target_status)
        if request.action == "approve":
            _start_payment(tracking_id, background_tasks)
        return {"tracking_id": tracking_id, "status": target_status, "result": "applied"}
    invoice = repository.get(tracking_id)
    return {"tracking_id": tracking_id, "status": invoice["status"], "result": "replayed"}


@app.post("/invoices/{tracking_id}/additional-info")
def additional_info(tracking_id: str, info: AdditionalInfo, background_tasks: BackgroundTasks):
    fields = {key: value for key, value in info.model_dump().items() if value is not None}
    outcome, invoice = get_repository().apply_additional_info(tracking_id, fields, _utc_now())
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Invoice not found")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="Additional information is only accepted after it was requested")
    if outcome == "duplicate":
        # the supplied invoice number matched an existing original: no re-evaluation
        logger.info("invoice %s marked duplicate of %s", tracking_id, invoice["duplicate_of"])
        return {"tracking_id": tracking_id, "status": "duplicate", "duplicate_of": invoice["duplicate_of"]}
    background_tasks.add_task(_publish_received, invoice)
    logger.info("additional info for %s, re-evaluating as revision %d", tracking_id, invoice["revision"])
    return {"tracking_id": tracking_id, "status": "received", "revision": invoice["revision"]}


_PAYMENT_RESULT_STATUSES = {"paid", "payment_failed"}


@app.post("/events/payment-result")
def handle_payment_result(event: dict):
    data = event.get("data") if isinstance(event, dict) else None
    if (
        not isinstance(data, dict)
        or not data.get("tracking_id")
        or not data.get("event_id")
        or not data.get("payment_id")
        or data.get("status") not in _PAYMENT_RESULT_STATUSES
    ):
        logger.error("dropping malformed payment.result event")
        return events.EVENT_DROP

    set_correlation_id(data.get("correlation_id", "-"))
    repository = get_repository()
    try:
        invoice = repository.get(data["tracking_id"])
        if invoice is None:
            logger.error("dropping payment result for unknown invoice %s", data["tracking_id"])
            return events.EVENT_DROP
        if invoice["status"] != "payment_requested":
            return events.EVENT_SUCCESS  # duplicate or late result; terminal states stay untouched

        summary = {
            "payment_id": data["payment_id"],
            "status": data["status"],
            "failure_reason": data.get("failure_reason"),
            "reservation_released": bool(data.get("reservation_released")),
            "requested_at": data.get("requested_at"),
            "executed_at": data.get("executed_at"),
            "released_at": data.get("released_at"),
        }
        repository.apply_payment_result(data["tracking_id"], data["status"], summary, _utc_now())
    except Exception:
        logger.exception("transient failure applying payment result for %s", data["tracking_id"])
        return events.EVENT_RETRY

    logger.info("invoice %s payment result: %s", data["tracking_id"], data["status"])
    return events.EVENT_SUCCESS


@app.get("/dapr/subscribe")
def dapr_subscribe():
    return [
        {
            "pubsubname": events.PUBSUB_NAME,
            "topic": events.TOPIC_INVOICE_DECIDED,
            "route": "/events/invoice-decided",
        },
        {
            "pubsubname": events.PUBSUB_NAME,
            "topic": events.TOPIC_PAYMENT_RESULT,
            "route": "/events/payment-result",
        },
    ]


@app.post("/events/invoice-decided")
def handle_invoice_decided(event: dict, background_tasks: BackgroundTasks):
    data = event.get("data") if isinstance(event, dict) else None
    if (
        not isinstance(data, dict)
        or not data.get("tracking_id")
        or not data.get("event_id")
        or data.get("status") not in _DECIDED_STATUSES
    ):
        logger.error("dropping malformed invoice.decided event")
        return events.EVENT_DROP

    set_correlation_id(data.get("correlation_id", "-"))
    repository = get_repository()
    try:
        invoice = repository.get(data["tracking_id"])
        if invoice is None:
            logger.error("dropping decision for unknown invoice %s", data["tracking_id"])
            return events.EVENT_DROP

        existing = invoice.get("decision")
        if existing and existing.get("event_id") == data["event_id"]:
            return events.EVENT_SUCCESS  # duplicate delivery
        if invoice["status"] != "received":
            return events.EVENT_SUCCESS  # already decided by another event

        event_revision = data.get("revision", 1)
        if event_revision != invoice.get("revision", 1):
            logger.info(
                "ignoring stale decision for %s (event revision %s, current %s)",
                data["tracking_id"], event_revision, invoice.get("revision", 1),
            )
            return events.EVENT_SUCCESS

        applied = repository.update_decision(
            data["tracking_id"], data["status"], data, _utc_now(), event_revision
        )
        if applied and data["status"] == "approved":
            _start_payment(data["tracking_id"], background_tasks)
    except Exception:
        logger.exception("transient failure applying decision for %s", data["tracking_id"])
        return events.EVENT_RETRY

    logger.info("invoice %s updated to %s (%s)", data["tracking_id"], data["status"], data.get("route"))
    return events.EVENT_SUCCESS
