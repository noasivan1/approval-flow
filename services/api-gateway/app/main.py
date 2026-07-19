import logging
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import ratelimit, workflow_client
from .config import settings
from .logging_config import configure_logging, set_correlation_id
from .models import (
    AdditionalInfo,
    AdditionalInfoResult,
    InvoiceSubmission,
    ReviewActionRequest,
    ReviewActionResult,
    SubmissionResult,
)

configure_logging(settings.log_level)
logger = logging.getLogger(settings.service_name)

app = FastAPI(
    title="ApprovalFlow API",
    version="1.0.0",
    description=(
        "Public API for invoice submission, human review, payments, and reporting. "
        "All access goes through this gateway; internal services are not exposed."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

limiter = ratelimit.build_limiter()

_RATE_LIMITED = {429: {"description": "Rate limit exceeded; retry after the given delay"}}


# registered before the correlation middleware so limited responses still
# pass through it and carry X-Correlation-ID
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    group = ratelimit.route_group(request.method, request.url.path)
    if limiter is None or group is None:
        return await call_next(request)
    client_id = request.client.host if request.client else "unknown"
    try:
        allowed, retry_after = await limiter.check(client_id, group)
    except ratelimit.RateLimitUnavailable:
        logger.exception("rate-limit store unavailable, refusing %s %s", request.method, request.url.path)
        return JSONResponse(status_code=503, content={"detail": "rate limiting unavailable"})
    if not allowed:
        logger.warning("rate limit exceeded for %s on %s", client_id, group)
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    set_correlation_id(correlation_id)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


@app.get("/health", summary="Service liveness")
async def health():
    """Simple liveness check; does not test downstream dependencies."""
    return {"service": settings.service_name, "status": "ok"}


@app.post(
    "/invoices",
    status_code=202,
    summary="Submit an invoice",
    response_model=SubmissionResult,
    response_model_exclude_none=True,
    responses=_RATE_LIMITED,
)
async def submit_invoice(submission: InvoiceSubmission):
    """Accepts an invoice for asynchronous processing and returns its tracking id.

    Duplicates are detected immediately: the reply then carries status
    `duplicate` and a link to the original invoice.
    """
    try:
        result = await workflow_client.submit_invoice(submission.model_dump())
    except httpx.HTTPError:
        logger.exception("invoice submission failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    response = {"tracking_id": result["tracking_id"], "status": result["status"]}
    if result.get("duplicate_of"):
        response["duplicate_of"] = result["duplicate_of"]
    return response


@app.get(
    "/invoices/{tracking_id}",
    summary="Look up an invoice",
    responses={404: {"description": "Unknown tracking id"}},
)
async def get_invoice(tracking_id: str):
    """Returns the submitted fields, current status, decision (with policy rules
    and provider details), payment summary, and review history."""
    try:
        invoice = await workflow_client.get_invoice(tracking_id)
    except httpx.HTTPError:
        logger.exception("invoice lookup failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@app.get("/approvals/queue", summary="List invoices waiting for human review")
async def approvals_queue():
    """Returns pending-review invoices, oldest first, each with the
    recommendation, confidence, rule ids, and plain-language reason."""
    try:
        return await workflow_client.get_approvals_queue()
    except httpx.HTTPError:
        logger.exception("approvals queue fetch failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")


@app.get("/dashboard", summary="Approval and payment totals")
async def dashboard():
    """Returns overall counts and USD amounts: invoices by status, auto-approved
    versus human-approved, escalations, duplicates, and payments."""
    try:
        return await workflow_client.get_dashboard()
    except httpx.HTTPError:
        logger.exception("dashboard fetch failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")


@app.get(
    "/invoices/{tracking_id}/trail",
    summary="Full processing history of one invoice",
    responses={404: {"description": "Unknown tracking id"}},
)
async def invoice_trail(tracking_id: str):
    """Returns one combined view: submitted data, correlation id, revision,
    decision details, review actions, duplicates, payment outcome, and
    notifications."""
    try:
        trail = await workflow_client.get_trail(tracking_id)
    except httpx.HTTPError:
        logger.exception("trail fetch failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    if trail is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return trail


@app.get("/notifications", summary="Outcome notifications for a submitter")
async def notifications(submitter_email: str):
    """Returns durable notifications for final invoice outcomes, newest first."""
    try:
        return await workflow_client.get_notifications(submitter_email)
    except httpx.HTTPError:
        logger.exception("notifications fetch failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")


@app.post(
    "/approvals/{tracking_id}/actions",
    summary="Approve, reject, or request more information",
    response_model=ReviewActionResult,
    responses={
        404: {"description": "Unknown tracking id"},
        409: {"description": "The invoice is no longer awaiting review"},
        **_RATE_LIMITED,
    },
)
async def review_action(tracking_id: str, request: ReviewActionRequest):
    """Applies one reviewer action to a pending-review invoice. Replaying the
    same idempotency key returns the recorded outcome instead of acting twice.
    """
    try:
        code, body = await workflow_client.submit_review_action(tracking_id, request.model_dump())
    except httpx.HTTPError:
        logger.exception("review action failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    if code in (404, 409):
        raise HTTPException(status_code=code, detail=body.get("detail", "Review action failed"))
    return body


@app.post(
    "/invoices/{tracking_id}/additional-info",
    summary="Add requested information to an invoice",
    response_model=AdditionalInfoResult,
    response_model_exclude_none=True,
    responses={
        404: {"description": "Unknown tracking id"},
        409: {"description": "The invoice is not waiting for more information"},
        **_RATE_LIMITED,
    },
)
async def additional_info(tracking_id: str, info: AdditionalInfo):
    """Accepts the structured details a reviewer asked for and sends the same
    invoice for another evaluation round, keeping its tracking id."""
    try:
        code, body = await workflow_client.submit_additional_info(tracking_id, info.model_dump())
    except httpx.HTTPError:
        logger.exception("additional info failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    if code in (404, 409):
        raise HTTPException(status_code=code, detail=body.get("detail", "Additional info rejected"))
    return body
