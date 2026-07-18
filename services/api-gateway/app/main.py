import logging
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import ratelimit, workflow_client
from .config import settings
from .logging_config import configure_logging, set_correlation_id
from .models import AdditionalInfo, InvoiceSubmission, ReviewActionRequest

configure_logging(settings.log_level)
logger = logging.getLogger(settings.service_name)

app = FastAPI(title=settings.service_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

limiter = ratelimit.build_limiter()


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


@app.get("/health")
async def health():
    return {"service": settings.service_name, "status": "ok"}


@app.post("/invoices", status_code=202)
async def submit_invoice(submission: InvoiceSubmission):
    try:
        result = await workflow_client.submit_invoice(submission.model_dump())
    except httpx.HTTPError:
        logger.exception("invoice submission failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    response = {"tracking_id": result["tracking_id"], "status": result["status"]}
    if result.get("duplicate_of"):
        response["duplicate_of"] = result["duplicate_of"]
    return response


@app.get("/invoices/{tracking_id}")
async def get_invoice(tracking_id: str):
    try:
        invoice = await workflow_client.get_invoice(tracking_id)
    except httpx.HTTPError:
        logger.exception("invoice lookup failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@app.get("/approvals/queue")
async def approvals_queue():
    try:
        return await workflow_client.get_approvals_queue()
    except httpx.HTTPError:
        logger.exception("approvals queue fetch failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")


@app.get("/dashboard")
async def dashboard():
    try:
        return await workflow_client.get_dashboard()
    except httpx.HTTPError:
        logger.exception("dashboard fetch failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")


@app.get("/invoices/{tracking_id}/trail")
async def invoice_trail(tracking_id: str):
    try:
        trail = await workflow_client.get_trail(tracking_id)
    except httpx.HTTPError:
        logger.exception("trail fetch failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    if trail is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return trail


@app.get("/notifications")
async def notifications(submitter_email: str):
    try:
        return await workflow_client.get_notifications(submitter_email)
    except httpx.HTTPError:
        logger.exception("notifications fetch failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")


@app.post("/approvals/{tracking_id}/actions")
async def review_action(tracking_id: str, request: ReviewActionRequest):
    try:
        code, body = await workflow_client.submit_review_action(tracking_id, request.model_dump())
    except httpx.HTTPError:
        logger.exception("review action failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    if code in (404, 409):
        raise HTTPException(status_code=code, detail=body.get("detail", "Review action failed"))
    return body


@app.post("/invoices/{tracking_id}/additional-info")
async def additional_info(tracking_id: str, info: AdditionalInfo):
    try:
        code, body = await workflow_client.submit_additional_info(tracking_id, info.model_dump())
    except httpx.HTTPError:
        logger.exception("additional info failed: workflow-service unreachable")
        raise HTTPException(status_code=502, detail="workflow-service unavailable")
    if code in (404, 409):
        raise HTTPException(status_code=code, detail=body.get("detail", "Additional info rejected"))
    return body
