import asyncio
import uuid

from fastapi.testclient import TestClient

from app import events, main
from app.main import app
from app.repository import get_repository

client = TestClient(app)

VALID_INVOICE = {
    "vendor": "Bistro 19",
    "amount": 42.0,
    "currency": "USD",
    "category": "meals",
    "receipt_present": True,
    "submitter_email": "dana.cohen@northwind.example",
    "department": "engineering-2026Q2",
    "vendor_known": True,
    "attendees": 1,
}


def decided_event(tracking_id, event_id=None, status="approved"):
    return {
        "data": {
            "event_id": event_id or str(uuid.uuid4()),
            "tracking_id": tracking_id,
            "correlation_id": "corr-1",
            "route": {"approved": "auto_approve", "pending_review": "human_review", "rejected": "reject"}[status],
            "status": status,
            "recommendation": "approve",
            "confidence": 0.95,
            "rule_ids": [],
            "reasons": [],
            "plain_language_reason": "Approved automatically: within policy and autonomy thresholds.",
            "amount_usd": 42.0,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        }
    }


def submit(monkeypatch, publish=None):
    async def default_publish(topic, data):
        pass

    monkeypatch.setattr(events, "publish", publish or default_publish)
    return client.post("/invoices", json=VALID_INVOICE).json()


def test_submission_publishes_received_event(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append((topic, data))

    monkeypatch.setattr(events, "publish", fake_publish)

    response = client.post(
        "/invoices", json=VALID_INVOICE, headers={"X-Correlation-ID": "corr-abc"}
    )
    assert response.status_code == 201
    tracking_id = response.json()["tracking_id"]

    assert len(published) == 1
    topic, event = published[0]
    assert topic == "invoice.received"
    assert event["tracking_id"] == tracking_id
    assert event["correlation_id"] == "corr-abc"
    assert event["invoice"]["vendor"] == "Bistro 19"
    # vendor_known is no longer a client field; the payload's copy is discarded
    assert "vendor_known" not in event["invoice"]

    # marked published, so the sweep will not resend it
    assert get_repository().get(tracking_id)["published_at"] is not None


def test_publish_failure_still_returns_created_and_sweep_recovers(monkeypatch):
    async def failing_publish(topic, data):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(events, "publish", failing_publish)

    response = client.post("/invoices", json=VALID_INVOICE)
    assert response.status_code == 201
    tracking_id = response.json()["tracking_id"]

    stored = get_repository().get(tracking_id)
    assert stored["status"] == "received"
    assert stored["published_at"] is None

    # sweep with a working publisher recovers the event
    republished = []

    async def working_publish(topic, data):
        republished.append(data)

    monkeypatch.setattr(events, "publish", working_publish)
    monkeypatch.setattr(main, "_SWEEP_GRACE_SECONDS", -5)  # include just-created rows
    count = asyncio.run(main.republish_pending())

    assert count >= 1
    assert any(event["tracking_id"] == tracking_id for event in republished)
    assert get_repository().get(tracking_id)["published_at"] is not None


def test_republished_event_is_identical(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append(data)

    invoice = submit(monkeypatch, fake_publish)
    event_again = main._received_event(get_repository().get(invoice["tracking_id"]))
    assert published[0] == event_again


def test_decided_event_updates_status_and_stores_decision(monkeypatch):
    invoice = submit(monkeypatch)
    response = client.post("/events/invoice-decided", json=decided_event(invoice["tracking_id"]))
    assert response.json() == {"status": "SUCCESS"}

    lookup = client.get(f"/invoices/{invoice['tracking_id']}").json()
    # approval immediately continues into the payment flow
    assert lookup["status"] == "payment_requested"
    decision = lookup["decision"]
    assert decision["route"] == "auto_approve"
    assert decision["confidence"] == 0.95
    assert decision["plain_language_reason"]
    assert "published_at" not in lookup


def test_duplicate_decided_event_is_harmless(monkeypatch):
    invoice = submit(monkeypatch)
    event = decided_event(invoice["tracking_id"], status="pending_review")

    first = client.post("/events/invoice-decided", json=event)
    second = client.post("/events/invoice-decided", json=event)
    assert first.json() == {"status": "SUCCESS"}
    assert second.json() == {"status": "SUCCESS"}

    lookup = client.get(f"/invoices/{invoice['tracking_id']}").json()
    assert lookup["status"] == "pending_review"


def test_conflicting_second_decision_does_not_overwrite(monkeypatch):
    invoice = submit(monkeypatch)
    client.post("/events/invoice-decided", json=decided_event(invoice["tracking_id"], status="approved"))
    response = client.post(
        "/events/invoice-decided", json=decided_event(invoice["tracking_id"], status="rejected")
    )
    assert response.json() == {"status": "SUCCESS"}

    lookup = client.get(f"/invoices/{invoice['tracking_id']}").json()
    assert lookup["status"] == "payment_requested"  # first decision won, then moved to payment


def test_transient_failure_returns_retry(monkeypatch):
    invoice = submit(monkeypatch)

    def failing_get(tracking_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(get_repository(), "get", failing_get)
    response = client.post("/events/invoice-decided", json=decided_event(invoice["tracking_id"]))
    assert response.json() == {"status": "RETRY"}
    monkeypatch.undo()

    # redelivery succeeds once the failure clears
    response = client.post("/events/invoice-decided", json=decided_event(invoice["tracking_id"]))
    assert response.json() == {"status": "SUCCESS"}


def test_malformed_decided_events_are_dropped():
    assert client.post("/events/invoice-decided", json={"id": "x"}).json() == {"status": "DROP"}
    assert (
        client.post(
            "/events/invoice-decided",
            json={"data": {"tracking_id": "t1", "event_id": "e1", "status": "bogus"}},
        ).json()
        == {"status": "DROP"}
    )


def test_decision_for_unknown_invoice_is_dropped():
    response = client.post("/events/invoice-decided", json=decided_event(str(uuid.uuid4())))
    assert response.json() == {"status": "DROP"}


def test_subscription_lists_decided_topic():
    response = client.get("/dapr/subscribe")
    assert response.json() == [
        {"pubsubname": "pubsub", "topic": "invoice.decided", "route": "/events/invoice-decided"},
        {"pubsubname": "pubsub", "topic": "payment.result", "route": "/events/payment-result"},
    ]
