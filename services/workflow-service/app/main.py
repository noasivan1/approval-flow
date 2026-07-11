import logging
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request

from .config import settings
from .logging_config import configure_logging, set_correlation_id
from .models import InvoiceSubmission
from .repository import get_repository

configure_logging(settings.log_level)
logger = logging.getLogger(settings.service_name)

app = FastAPI(title=settings.service_name)


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


@app.post("/invoices", status_code=201)
def submit_invoice(submission: InvoiceSubmission):
    now = datetime.now(timezone.utc).isoformat()
    invoice = {
        "tracking_id": str(uuid.uuid4()),
        "status": "received",
        **submission.model_dump(),
        "created_at": now,
        "updated_at": now,
    }
    get_repository().save(invoice)
    logger.info("invoice received: tracking_id=%s vendor=%s", invoice["tracking_id"], invoice["vendor"])
    return invoice


@app.get("/invoices/{tracking_id}")
def get_invoice(tracking_id: str):
    invoice = get_repository().get(tracking_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice
