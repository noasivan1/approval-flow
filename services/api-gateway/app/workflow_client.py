import httpx

from .config import settings
from .logging_config import correlation_id_var

_TIMEOUT = 10.0


def _headers() -> dict:
    # forward the caller's correlation id
    return {"X-Correlation-ID": correlation_id_var.get()}


async def submit_invoice(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"{settings.workflow_service_url}/invoices",
            json=payload,
            headers=_headers(),
        )
    response.raise_for_status()
    return response.json()


async def get_invoice(tracking_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{settings.workflow_service_url}/invoices/{tracking_id}",
            headers=_headers(),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()
