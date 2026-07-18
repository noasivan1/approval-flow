import uuid

from fastapi.testclient import TestClient

from app import events
from app.main import app

client = TestClient(app)


def payload(**overrides):
    data = {
        "vendor": "Bistro 19",
        "amount": 42.0,
        "currency": "USD",
        "category": "meals",
        "receipt_present": True,
        "submitter_email": "dash@northwind.example",
        "department": "engineering-2026Q2",
        "vendor_known": True,
        "attendees": 1,
    }
    data.update(overrides)
    return data


def decided_event(tracking_id, status, route=None, amount_usd=42.0):
    default_route = {"approved": "auto_approve", "pending_review": "human_review", "rejected": "reject"}
    return {
        "data": {
            "event_id": str(uuid.uuid4()),
            "tracking_id": tracking_id,
            "correlation_id": "corr-dash",
            "revision": 1,
            "route": route or default_route[status],
            "status": status,
            "recommendation": "review",
            "confidence": 0.9,
            "rule_ids": ["AUTONOMY-CEILING"],
            "reasons": [{"rule_id": "AUTONOMY-CEILING", "code": "over_ceiling", "detail": "over"}],
            "plain_language_reason": "reason",
            "amount_usd": amount_usd,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        }
    }


def approve(tracking_id):
    return client.post(
        f"/approvals/{tracking_id}/actions",
        json={
            "action": "approve",
            "reviewer_email": "finance@northwind.example",
            "comment": None,
            "idempotency_key": str(uuid.uuid4()),
        },
    )


def patch_publisher(monkeypatch):
    async def fake_publish(topic, data):
        pass

    monkeypatch.setattr(events, "publish", fake_publish)


def test_dashboard_counts_and_amounts(monkeypatch):
    patch_publisher(monkeypatch)
    before = client.get("/dashboard").json()

    # one auto-approved invoice ($42)
    auto = client.post("/invoices", json=payload()).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(auto, "approved", amount_usd=42.0))

    # one human-approved invoice ($300)
    human = client.post("/invoices", json=payload(amount=300.0)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(human, "pending_review", amount_usd=300.0))
    approve(human)

    # one rejected, one duplicate
    rejected = client.post("/invoices", json=payload()).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(rejected, "rejected"))
    dup_payload = payload(invoice_number=f"NW-{uuid.uuid4().hex[:8]}")
    client.post("/invoices", json=dup_payload)
    client.post("/invoices", json=dup_payload)

    after = client.get("/dashboard").json()
    assert after["total_invoices"] == before["total_invoices"] + 5
    assert after["auto_approved_count"] == before["auto_approved_count"] + 1
    assert after["human_approved_count"] == before["human_approved_count"] + 1
    assert after["escalated_count"] == before["escalated_count"] + 1
    assert after["duplicate_count"] == before["duplicate_count"] + 1
    assert round(after["auto_approved_amount_usd"] - before["auto_approved_amount_usd"], 2) == 42.0
    assert round(after["human_approved_amount_usd"] - before["human_approved_amount_usd"], 2) == 300.0
    assert "status_counts" in after


def test_approval_categories_do_not_overlap(monkeypatch):
    patch_publisher(monkeypatch)
    before = client.get("/dashboard").json()

    tracking_id = client.post("/invoices", json=payload(amount=300.0)).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id, "pending_review"))
    approve(tracking_id)

    after = client.get("/dashboard").json()
    # a human-approved invoice counts once, never as auto-approved
    assert after["human_approved_count"] == before["human_approved_count"] + 1
    assert after["auto_approved_count"] == before["auto_approved_count"]


def test_trail_contains_full_history(monkeypatch):
    patch_publisher(monkeypatch)
    invoice_number = f"NW-{uuid.uuid4().hex[:8]}"
    submission = payload(amount=300.0, invoice_number=invoice_number, note="trail test")
    tracking_id = client.post(
        "/invoices", json=submission, headers={"X-Correlation-ID": "corr-trail"}
    ).json()["tracking_id"]
    client.post("/events/invoice-decided", json=decided_event(tracking_id, "pending_review"))
    approve(tracking_id)
    client.post(
        "/events/payment-result",
        json={
            "data": {
                "event_id": str(uuid.uuid4()),
                "tracking_id": tracking_id,
                "correlation_id": "corr-trail",
                "payment_id": "pay-trail",
                "status": "paid",
                "failure_reason": None,
                "reservation_released": False,
                "requested_at": "2026-01-01T00:00:00+00:00",
                "executed_at": "2026-01-01T00:00:01+00:00",
                "released_at": None,
            }
        },
    )
    # a duplicate pointing at this invoice
    duplicate = client.post("/invoices", json=submission).json()["tracking_id"]

    trail = client.get(f"/invoices/{tracking_id}/trail").json()
    assert trail["tracking_id"] == tracking_id
    assert trail["correlation_id"] == "corr-trail"
    assert trail["revision"] == 1
    assert trail["submission"]["vendor"] == "Bistro 19"
    assert trail["submission"]["note"] == "trail test"
    assert trail["decision"]["rule_ids"] == ["AUTONOMY-CEILING"]
    assert trail["decision"]["recommendation"] == "review"
    assert trail["decision"]["confidence"] == 0.9
    assert len(trail["review_actions"]) == 1
    assert trail["review_actions"][0]["action"] == "approve"
    assert trail["duplicates"] == [duplicate]
    assert trail["payment"]["payment_id"] == "pay-trail"
    assert any(n["status"] == "paid" for n in trail["notifications"])

    # the duplicate's own trail links back
    dup_trail = client.get(f"/invoices/{duplicate}/trail").json()
    assert dup_trail["duplicate_of"] == tracking_id
    assert dup_trail["decision"]["rule_ids"] == ["GLOBAL-DUP"]


def test_trail_unknown_invoice_returns_404():
    response = client.get(f"/invoices/{uuid.uuid4()}/trail")
    assert response.status_code == 404
