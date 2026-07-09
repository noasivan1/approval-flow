import uuid

from fastapi import FastAPI, Request

from .config import settings
from .logging_config import configure_logging, set_correlation_id

configure_logging(settings.log_level)

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
