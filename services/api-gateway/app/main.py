import logging
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from . import workflow_client
from .config import settings
from .logging_config import configure_logging, set_correlation_id
from .models import InvoiceSubmission

configure_logging(settings.log_level)
logger = logging.getLogger(settings.service_name)

app = FastAPI(title=settings.service_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    return {"tracking_id": result["tracking_id"], "status": result["status"]}


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
