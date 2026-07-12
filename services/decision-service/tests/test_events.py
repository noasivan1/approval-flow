import uuid

from fastapi.testclient import TestClient

from app import events, main
from app.main import app

client = TestClient(app)

INVOICE = {
    "vendor": "Bistro 19",
    "amount": 42.0,
    "currency": "USD",
    "category": "meals",
    "receipt_present": True,
    "submitter_email": "dana.cohen@northwind.example",
    "vendor_known": True,
    "attendees": 1,
}


def received_event(event_id=None, tracking_id=None):
    return {
        "data": {
            "event_id": event_id or str(uuid.uuid4()),
            "tracking_id": tracking_id or str(uuid.uuid4()),
            "correlation_id": "test-corr",
            "invoice": dict(INVOICE),
        }
    }


def test_successful_processing_publishes_decision(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append((topic, data))

    monkeypatch.setattr(events, "publish", fake_publish)

    response = client.post("/events/invoice-received", json=received_event())
    assert response.status_code == 200
    assert response.json() == {"status": "SUCCESS"}

    assert len(published) == 1
    topic, decided = published[0]
    assert topic == "invoice.decided"
    for field in (
        "event_id", "tracking_id", "correlation_id", "route", "status",
        "recommendation", "confidence", "rule_ids", "reasons",
        "plain_language_reason", "evaluated_at",
    ):
        assert field in decided
    assert decided["route"] == "auto_approve"
    assert decided["correlation_id"] == "test-corr"


def test_duplicate_delivery_publishes_once(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append(data)

    monkeypatch.setattr(events, "publish", fake_publish)

    event = received_event()
    first = client.post("/events/invoice-received", json=event)
    second = client.post("/events/invoice-received", json=event)

    assert first.json() == {"status": "SUCCESS"}
    assert second.json() == {"status": "SUCCESS"}
    assert len(published) == 1


def test_publish_failure_returns_retry_and_event_not_marked_seen(monkeypatch):
    calls = {"count": 0}

    async def failing_publish(topic, data):
        calls["count"] += 1
        raise RuntimeError("sidecar unavailable")

    monkeypatch.setattr(events, "publish", failing_publish)

    event = received_event()
    response = client.post("/events/invoice-received", json=event)
    assert response.json() == {"status": "RETRY"}
    assert event["data"]["event_id"] not in main._seen_ids

    # redelivery after recovery processes the same event successfully
    published = []

    async def working_publish(topic, data):
        published.append(data)

    monkeypatch.setattr(events, "publish", working_publish)
    response = client.post("/events/invoice-received", json=event)
    assert response.json() == {"status": "SUCCESS"}
    assert len(published) == 1


def test_redelivered_event_produces_identical_event_id(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append(data)

    monkeypatch.setattr(events, "publish", fake_publish)

    event = received_event()
    client.post("/events/invoice-received", json=event)
    main._seen_ids.discard(event["data"]["event_id"])  # simulate instance restart
    client.post("/events/invoice-received", json=event)

    assert len(published) == 2
    assert published[0]["event_id"] == published[1]["event_id"]


def test_malformed_payload_is_dropped(monkeypatch):
    async def fake_publish(topic, data):
        raise AssertionError("must not publish for malformed events")

    monkeypatch.setattr(events, "publish", fake_publish)

    no_data = client.post("/events/invoice-received", json={"id": "x"})
    assert no_data.json() == {"status": "DROP"}

    bad_invoice = client.post(
        "/events/invoice-received",
        json={"data": {"event_id": "e1", "tracking_id": "t1", "invoice": {"amount": -5}}},
    )
    assert bad_invoice.json() == {"status": "DROP"}

    missing_ids = client.post(
        "/events/invoice-received",
        json={"data": {"invoice": dict(INVOICE)}},
    )
    assert missing_ids.json() == {"status": "DROP"}


def test_revision_is_echoed_into_decided_event(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append(data)

    monkeypatch.setattr(events, "publish", fake_publish)

    event = received_event()
    event["data"]["revision"] = 3
    client.post("/events/invoice-received", json=event)
    assert published[0]["revision"] == 3

    # events without a revision default to the first round
    client.post("/events/invoice-received", json=received_event())
    assert published[1]["revision"] == 1


def test_subscription_lists_received_topic():
    response = client.get("/dapr/subscribe")
    subscriptions = response.json()
    assert subscriptions == [
        {"pubsubname": "pubsub", "topic": "invoice.received", "route": "/events/invoice-received"}
    ]
