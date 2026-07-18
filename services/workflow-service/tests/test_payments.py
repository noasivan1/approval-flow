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
    "vendor_known": True,
    "attendees": 1,
    "department": "engineering-2026Q2",
}


def patch_publisher(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append((topic, data))

    monkeypatch.setattr(events, "publish", fake_publish)
    return published


def decided_event(tracking_id, status="approved", revision=1):
    route = {"approved": "auto_approve", "pending_review": "human_review", "rejected": "reject"}[status]
    return {
        "data": {
            "event_id": str(uuid.uuid4()),
            "tracking_id": tracking_id,
            "correlation_id": "corr-pay",
            "revision": revision,
            "route": route,
            "status": status,
            "recommendation": "approve",
            "confidence": 0.95,
            "rule_ids": [],
            "reasons": [],
            "plain_language_reason": "Approved automatically.",
            "amount_usd": 42.0,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        }
    }


def result_event(tracking_id, status="paid", **overrides):
    data = {
        "event_id": str(uuid.uuid4()),
        "tracking_id": tracking_id,
        "correlation_id": "corr-pay",
        "payment_id": f"pay-{tracking_id[:8]}",
        "status": status,
        "failure_reason": None if status == "paid" else "provider_declined",
        "reservation_released": status != "paid",
        "requested_at": "2026-01-01T00:00:00+00:00",
        "executed_at": "2026-01-01T00:00:01+00:00" if status == "paid" else None,
        "released_at": None if status == "paid" else "2026-01-01T00:00:01+00:00",
    }
    data.update(overrides)
    return {"data": data}


def payment_requests(published):
    return [event for topic, event in published if topic == "payment.requested"]


def submit(monkeypatch, published=None):
    monkeypatch.setattr(events, "publish", _collector(published))
    return client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]


def _collector(published):
    async def fake_publish(topic, data):
        if published is not None:
            published.append((topic, data))

    return fake_publish


def test_auto_approval_starts_exactly_one_payment(monkeypatch):
    published = patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]

    client.post("/events/invoice-decided", json=decided_event(tracking_id))
    requests = payment_requests(published)
    assert len(requests) == 1
    event = requests[0]
    assert event["tracking_id"] == tracking_id
    assert event["amount_usd"] == 42.0
    assert event["department"] == "engineering-2026Q2"
    assert event["correlation_id"]

    invoice = client.get(f"/invoices/{tracking_id}").json()
    assert invoice["status"] == "payment_requested"

    # a redelivered decided event cannot start a second payment
    client.post("/events/invoice-decided", json=decided_event(tracking_id))
    assert len(payment_requests(published)) == 1


def test_human_approval_uses_same_payment_flow(monkeypatch):
    published = patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id, status="pending_review"))

    payload = {
        "action": "approve",
        "reviewer_email": "finance@northwind.example",
        "comment": None,
        "idempotency_key": str(uuid.uuid4()),
    }
    client.post(f"/approvals/{tracking_id}/actions", json=payload)
    assert len(payment_requests(published)) == 1

    # replaying the approve action publishes nothing new
    client.post(f"/approvals/{tracking_id}/actions", json=payload)
    assert len(payment_requests(published)) == 1


def test_non_approved_statuses_never_trigger_payment(monkeypatch):
    published = patch_publisher(monkeypatch)
    received = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]

    pending = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(pending, status="pending_review"))

    rejected = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(rejected, status="rejected"))

    more_info = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(more_info, status="pending_review"))
    client.post(
        f"/approvals/{more_info}/actions",
        json={
            "action": "request_more_info",
            "reviewer_email": "finance@northwind.example",
            "comment": "Need details",
            "idempotency_key": str(uuid.uuid4()),
        },
    )

    dup_payload = {**VALID_INVOICE, "invoice_number": f"NW-{uuid.uuid4().hex[:8]}"}
    client.post("/invoices", json=dup_payload)
    duplicate = client.post("/invoices", json=dup_payload).json()["tracking_id"]

    assert payment_requests(published) == []
    for tracking_id, status in [
        (received, "received"),
        (pending, "pending_review"),
        (rejected, "rejected"),
        (more_info, "needs_more_info"),
        (duplicate, "duplicate"),
    ]:
        assert client.get(f"/invoices/{tracking_id}").json()["status"] == status


def test_old_approved_row_without_flow_version_is_not_paid(monkeypatch):
    published = patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]

    # simulate a pre-feature approved row: approved but no payment_flow_version
    record = get_repository()._invoices[tracking_id]
    record["status"] = "approved"
    record["payment_flow_version"] = None

    monkeypatch.setattr(main, "_SWEEP_GRACE_SECONDS", -5)
    asyncio.run(main.republish_pending())
    assert all(event["tracking_id"] != tracking_id for event in payment_requests(published))
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "approved"


def test_sweep_recovers_stuck_approved_invoice(monkeypatch):
    published = patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id))

    # simulate a crash between approval and the payment flip
    record = get_repository()._invoices[tracking_id]
    record["status"] = "approved"
    published.clear()

    monkeypatch.setattr(main, "_SWEEP_GRACE_SECONDS", -5)
    count = asyncio.run(main.republish_pending())
    assert count >= 1
    assert any(event["tracking_id"] == tracking_id for event in payment_requests(published))
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "payment_requested"


def test_sweep_republishes_unpublished_payment_request(monkeypatch):
    async def failing_publish(topic, data):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(events, "publish", failing_publish)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    get_repository().mark_published(tracking_id, main._utc_now())  # received event out of the way
    client.post("/events/invoice-decided", json=decided_event(tracking_id))

    invoice = get_repository().get(tracking_id)
    assert invoice["status"] == "payment_requested"
    assert invoice["payment_published_at"] is None

    published = patch_publisher(monkeypatch)
    monkeypatch.setattr(main, "_SWEEP_GRACE_SECONDS", -5)
    asyncio.run(main.republish_pending())
    assert len(payment_requests(published)) == 1
    assert get_repository().get(tracking_id)["payment_published_at"] is not None

    # stable event id: the republished event matches a fresh build
    assert payment_requests(published)[0]["event_id"] == main._payment_requested_event(
        get_repository().get(tracking_id)
    )["event_id"]


def test_received_sweep_still_works(monkeypatch):
    async def failing_publish(topic, data):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(events, "publish", failing_publish)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    assert get_repository().get(tracking_id)["published_at"] is None

    published = patch_publisher(monkeypatch)
    monkeypatch.setattr(main, "_SWEEP_GRACE_SECONDS", -5)
    asyncio.run(main.republish_pending())
    received = [event for topic, event in published if topic == "invoice.received"]
    assert any(event["tracking_id"] == tracking_id for event in received)


def test_paid_result_updates_invoice_once(monkeypatch):
    patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id))

    event = result_event(tracking_id, status="paid")
    response = client.post("/events/payment-result", json=event)
    assert response.json() == {"status": "SUCCESS"}

    invoice = client.get(f"/invoices/{tracking_id}").json()
    assert invoice["status"] == "paid"
    assert invoice["payment"]["payment_id"] == event["data"]["payment_id"]
    assert invoice["payment"]["status"] == "paid"

    # duplicate delivery is harmless
    assert client.post("/events/payment-result", json=event).json() == {"status": "SUCCESS"}
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "paid"


def test_failed_result_updates_invoice_once(monkeypatch):
    patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id))

    client.post("/events/payment-result", json=result_event(tracking_id, status="payment_failed"))
    invoice = client.get(f"/invoices/{tracking_id}").json()
    assert invoice["status"] == "payment_failed"
    assert invoice["payment"]["failure_reason"] == "provider_declined"
    assert invoice["payment"]["reservation_released"] is True


def test_late_result_cannot_overwrite_terminal_state(monkeypatch):
    patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id))
    client.post("/events/payment-result", json=result_event(tracking_id, status="paid"))

    late = client.post("/events/payment-result", json=result_event(tracking_id, status="payment_failed"))
    assert late.json() == {"status": "SUCCESS"}
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "paid"


def test_stale_decision_cannot_restart_payment(monkeypatch):
    published = patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id))
    client.post("/events/payment-result", json=result_event(tracking_id, status="paid"))
    before = len(payment_requests(published))

    stale = client.post("/events/invoice-decided", json=decided_event(tracking_id, revision=1))
    assert stale.json() == {"status": "SUCCESS"}
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "paid"
    assert len(payment_requests(published)) == before


def test_malformed_payment_result_is_dropped():
    assert client.post("/events/payment-result", json={"id": "x"}).json() == {"status": "DROP"}
    assert (
        client.post(
            "/events/payment-result",
            json={"data": {"tracking_id": "t", "event_id": "e", "payment_id": "p", "status": "bogus"}},
        ).json()
        == {"status": "DROP"}
    )


def test_result_for_unknown_invoice_is_dropped():
    response = client.post("/events/payment-result", json=result_event(str(uuid.uuid4())))
    assert response.json() == {"status": "DROP"}


def test_transient_failure_on_result_returns_retry(monkeypatch):
    patch_publisher(monkeypatch)
    tracking_id = client.post("/invoices", json=VALID_INVOICE).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id))

    def failing_get(tid):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(get_repository(), "get", failing_get)
    response = client.post("/events/payment-result", json=result_event(tracking_id))
    assert response.json() == {"status": "RETRY"}
    monkeypatch.undo()

    response = client.post("/events/payment-result", json=result_event(tracking_id))
    assert response.json() == {"status": "SUCCESS"}
