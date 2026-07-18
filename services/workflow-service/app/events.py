import os

import httpx

PUBSUB_NAME = "pubsub"
TOPIC_INVOICE_RECEIVED = "invoice.received"
TOPIC_INVOICE_DECIDED = "invoice.decided"
TOPIC_PAYMENT_REQUESTED = "payment.requested"
TOPIC_PAYMENT_RESULT = "payment.result"

# Dapr pub/sub acknowledgements
EVENT_SUCCESS = {"status": "SUCCESS"}
EVENT_RETRY = {"status": "RETRY"}
EVENT_DROP = {"status": "DROP"}

_TIMEOUT = 5.0


async def publish(topic: str, data: dict) -> None:
    port = os.getenv("DAPR_HTTP_PORT", "3500")
    url = f"http://localhost:{port}/v1.0/publish/{PUBSUB_NAME}/{topic}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(url, json=data)
    response.raise_for_status()
