import asyncio
import uuid

from fastapi.testclient import TestClient

from app import events, main
from app.main import app
from app.repository import get_repository

client = TestClient(app)


def payload(email=None, **overrides):
    data = {
        "vendor": "Bistro 19",
        "amount": 42.0,
        "currency": "USD",
        "category": "meals",
        "receipt_present": True,
        "submitter_email": email or f"{uuid.uuid4().hex[:8]}@northwind.example",
        "department": "engineering-2026Q2",
        "vendor_known": True,
        "attendees": 1,
    }
    data.update(overrides)
    return data


def decided_event(tracking_id, status, revision=1):
    route = {"approved": "auto_approve", "pending_review": "human_review", "rejected": "reject"}[status]
    return {
        "data": {
            "event_id": str(uuid.uuid4()),
            "tracking_id": tracking_id,
            "correlation_id": "corr-n",
            "revision": revision,
            "route": route,
            "status": status,
            "recommendation": "review",
            "confidence": 0.9,
            "rule_ids": [],
            "reasons": [],
            "plain_language_reason": "reason",
            "amount_usd": 42.0,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        }
    }


def result_event(tracking_id, status="paid"):
    return {
        "data": {
            "event_id": str(uuid.uuid4()),
            "tracking_id": tracking_id,
            "correlation_id": "corr-n",
            "payment_id": f"pay-{uuid.uuid4().hex[:6]}",
            "status": status,
            "failure_reason": None if status == "paid" else "provider_declined",
            "reservation_released": status != "paid",
            "requested_at": "2026-01-01T00:00:00+00:00",
            "executed_at": "2026-01-01T00:00:01+00:00",
            "released_at": None,
        }
    }


def notifications_for(email):
    return client.get("/notifications", params={"submitter_email": email}).json()


def patch_publisher(monkeypatch):
    async def fake_publish(topic, data):
        pass

    monkeypatch.setattr(events, "publish", fake_publish)


def test_paid_creates_notification(monkeypatch):
    patch_publisher(monkeypatch)
    email = f"paid-{uuid.uuid4().hex[:6]}@x.example"
    tracking_id = client.post("/invoices", json=payload(email)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id, "approved"))
    client.post("/events/payment-result", json=result_event(tracking_id, "paid"))

    items = notifications_for(email)
    assert len(items) == 1
    assert items[0]["status"] == "paid"
    assert items[0]["tracking_id"] == tracking_id
    assert "paid" in items[0]["message"]


def test_payment_failed_rejected_and_needs_more_info_notify(monkeypatch):
    patch_publisher(monkeypatch)

    email1 = f"fail-{uuid.uuid4().hex[:6]}@x.example"
    t1 = client.post("/invoices", json=payload(email1)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(t1, "approved"))
    client.post("/events/payment-result", json=result_event(t1, "payment_failed"))
    assert [n["status"] for n in notifications_for(email1)] == ["payment_failed"]

    email2 = f"rej-{uuid.uuid4().hex[:6]}@x.example"
    t2 = client.post("/invoices", json=payload(email2)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(t2, "rejected"))
    assert [n["status"] for n in notifications_for(email2)] == ["rejected"]

    email3 = f"info-{uuid.uuid4().hex[:6]}@x.example"
    t3 = client.post("/invoices", json=payload(email3)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(t3, "pending_review"))
    client.post(
        f"/approvals/{t3}/actions",
        json={
            "action": "request_more_info",
            "reviewer_email": "finance@northwind.example",
            "comment": "Need details",
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert [n["status"] for n in notifications_for(email3)] == ["needs_more_info"]


def test_duplicate_creates_notification(monkeypatch):
    patch_publisher(monkeypatch)
    email = f"dup-{uuid.uuid4().hex[:6]}@x.example"
    dup_payload = payload(email, invoice_number=f"NW-{uuid.uuid4().hex[:8]}")
    client.post("/invoices", json=dup_payload)
    client.post("/invoices", json=dup_payload)

    items = notifications_for(email)
    assert [n["status"] for n in items] == ["duplicate"]


def test_intermediate_statuses_do_not_notify(monkeypatch):
    patch_publisher(monkeypatch)
    email = f"mid-{uuid.uuid4().hex[:6]}@x.example"
    tracking_id = client.post("/invoices", json=payload(email)).json()["tracking_id"]
    assert notifications_for(email) == []  # received

    client.post("/events/invoice-decided", json=decided_event(tracking_id, "approved"))
    assert notifications_for(email) == []  # approved / payment_requested


def test_duplicate_notification_attempts_are_harmless(monkeypatch):
    patch_publisher(monkeypatch)
    email = f"once-{uuid.uuid4().hex[:6]}@x.example"
    tracking_id = client.post("/invoices", json=payload(email)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id, "approved"))
    event = result_event(tracking_id, "paid")
    client.post("/events/payment-result", json=event)
    client.post("/events/payment-result", json=event)  # replay
    # direct repeated write is also a no-op
    main._notify(get_repository().get(tracking_id))

    assert len(notifications_for(email)) == 1


def test_notification_failure_does_not_roll_back_status(monkeypatch):
    patch_publisher(monkeypatch)
    email = f"nofail-{uuid.uuid4().hex[:6]}@x.example"
    tracking_id = client.post("/invoices", json=payload(email)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id, "approved"))

    def failing_add(record):
        raise RuntimeError("notifications table unavailable")

    monkeypatch.setattr(get_repository(), "add_notification", failing_add)
    response = client.post("/events/payment-result", json=result_event(tracking_id, "paid"))
    assert response.json() == {"status": "SUCCESS"}
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "paid"
    monkeypatch.undo()

    # recovery sweep creates the missing notification
    monkeypatch.setattr(main, "_SWEEP_GRACE_SECONDS", -5)
    asyncio.run(main.republish_pending())
    items = notifications_for(email)
    assert [n["status"] for n in items] == ["paid"]

    # a second sweep does not duplicate it
    asyncio.run(main.republish_pending())
    assert len(notifications_for(email)) == 1


def test_notifications_ordered_newest_first(monkeypatch):
    patch_publisher(monkeypatch)
    email = f"order-{uuid.uuid4().hex[:6]}@x.example"

    t1 = client.post("/invoices", json=payload(email)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(t1, "rejected"))

    t2 = client.post("/invoices", json=payload(email)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(t2, "approved"))
    client.post("/events/payment-result", json=result_event(t2, "paid"))

    items = notifications_for(email)
    assert len(items) == 2
    assert items[0]["created_at"] >= items[1]["created_at"]
    assert items[0]["tracking_id"] == t2  # newest first
