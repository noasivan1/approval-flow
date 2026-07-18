import uuid

from fastapi.testclient import TestClient

from app import events
from app.main import app
from app.repository import get_repository

client = TestClient(app)

VALID_INVOICE = {
    "vendor": "PixelForge",
    "amount": 300.0,
    "currency": "USD",
    "category": "saas",
    "receipt_present": True,
    "submitter_email": "omar.farouk@northwind.example",
    "department": "engineering-2026Q2",
    "vendor_known": True,
}


def decided_event(tracking_id, status="pending_review", revision=1, event_id=None):
    route = {"approved": "auto_approve", "pending_review": "human_review", "rejected": "reject"}[status]
    return {
        "data": {
            "event_id": event_id or str(uuid.uuid4()),
            "tracking_id": tracking_id,
            "correlation_id": "corr-review",
            "revision": revision,
            "route": route,
            "status": status,
            "recommendation": "review",
            "confidence": 0.95,
            "rule_ids": ["AUTONOMY-CEILING"],
            "reasons": [{"rule_id": "AUTONOMY-CEILING", "code": "over_ceiling", "detail": "over ceiling"}],
            "plain_language_reason": "Escalated for human review: over ceiling.",
            "amount_usd": 300.0,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        }
    }


def submit_pending_review(monkeypatch, published=None):
    """Submit an invoice and route it to pending_review."""

    async def fake_publish(topic, data):
        if published is not None:
            published.append((topic, data))

    monkeypatch.setattr(events, "publish", fake_publish)
    invoice = client.post("/invoices", json=VALID_INVOICE).json()
    client.post("/events/invoice-decided", json=decided_event(invoice["tracking_id"]))
    return invoice["tracking_id"]


def action_payload(action="approve", comment=None, key=None):
    return {
        "action": action,
        "reviewer_email": "finance@northwind.example",
        "comment": comment,
        "idempotency_key": key or str(uuid.uuid4()),
    }


def test_queue_contains_pending_review_only(monkeypatch):
    pending_id = submit_pending_review(monkeypatch)

    # an invoice still awaiting decision must not appear
    received = client.post("/invoices", json=VALID_INVOICE).json()

    queue = client.get("/approvals/queue").json()
    ids = [item["tracking_id"] for item in queue]
    assert pending_id in ids
    assert received["tracking_id"] not in ids

    item = next(item for item in queue if item["tracking_id"] == pending_id)
    for field in (
        "vendor", "amount", "currency", "category", "recommendation",
        "confidence", "rule_ids", "plain_language_reason", "review_requested_at",
    ):
        assert field in item
    assert item["review_requested_at"] is not None
    assert item["rule_ids"] == ["AUTONOMY-CEILING"]


def test_auto_approved_invoice_never_queued(monkeypatch):
    async def fake_publish(topic, data):
        pass

    monkeypatch.setattr(events, "publish", fake_publish)
    invoice = client.post("/invoices", json=VALID_INVOICE).json()
    client.post(
        "/events/invoice-decided",
        json=decided_event(invoice["tracking_id"], status="approved"),
    )

    ids = [item["tracking_id"] for item in client.get("/approvals/queue").json()]
    assert invoice["tracking_id"] not in ids


def test_approve_moves_to_approved(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    response = client.post(f"/approvals/{tracking_id}/actions", json=action_payload("approve"))
    assert response.status_code == 200
    assert response.json() == {"tracking_id": tracking_id, "status": "approved", "result": "applied"}
    # approval immediately continues into the payment flow
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "payment_requested"


def test_reject_moves_to_rejected(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    response = client.post(f"/approvals/{tracking_id}/actions", json=action_payload("reject"))
    assert response.json()["status"] == "rejected"


def test_request_more_info_moves_to_needs_more_info(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    response = client.post(
        f"/approvals/{tracking_id}/actions",
        json=action_payload("request_more_info", comment="Which client was this for?"),
    )
    assert response.json()["status"] == "needs_more_info"

    invoice = client.get(f"/invoices/{tracking_id}").json()
    assert invoice["status"] == "needs_more_info"
    assert invoice["review_actions"][0]["comment"] == "Which client was this for?"


def test_request_more_info_requires_comment(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    response = client.post(f"/approvals/{tracking_id}/actions", json=action_payload("request_more_info"))
    assert response.status_code == 422


def test_review_actions_stored_durably(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    client.post(f"/approvals/{tracking_id}/actions", json=action_payload("approve", comment="Looks fine"))

    actions = get_repository().list_review_actions(tracking_id)
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "approve"
    assert action["reviewer_email"] == "finance@northwind.example"
    assert action["revision"] == 1
    assert action["action_id"]
    assert action["created_at"]


def test_idempotency_key_replay_creates_single_action(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    payload = action_payload("approve", key="key-123")

    first = client.post(f"/approvals/{tracking_id}/actions", json=payload)
    second = client.post(f"/approvals/{tracking_id}/actions", json=payload)

    assert first.json()["result"] == "applied"
    assert second.status_code == 200
    # the replay reports the live status, which has moved on to payment
    assert second.json() == {"tracking_id": tracking_id, "status": "payment_requested", "result": "replayed"}
    assert len(get_repository().list_review_actions(tracking_id)) == 1


def test_action_after_leaving_pending_review_conflicts(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    client.post(f"/approvals/{tracking_id}/actions", json=action_payload("approve"))

    late = client.post(f"/approvals/{tracking_id}/actions", json=action_payload("reject"))
    assert late.status_code == 409
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "payment_requested"


def test_action_on_unknown_invoice_returns_404():
    response = client.post(f"/approvals/{uuid.uuid4()}/actions", json=action_payload("approve"))
    assert response.status_code == 404


def test_additional_info_only_accepted_in_needs_more_info(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    response = client.post(f"/invoices/{tracking_id}/additional-info", json={"attendees": 2})
    assert response.status_code == 409


def test_additional_info_resubmits_with_incremented_revision(monkeypatch):
    published = []
    tracking_id = submit_pending_review(monkeypatch, published)
    client.post(
        f"/approvals/{tracking_id}/actions",
        json=action_payload("request_more_info", comment="Need the client name"),
    )

    response = client.post(
        f"/invoices/{tracking_id}/additional-info",
        json={"client_name": "Northwind", "business_justification": "contract renewal dinner"},
    )
    assert response.status_code == 200
    assert response.json() == {"tracking_id": tracking_id, "status": "received", "revision": 2}

    invoice = client.get(f"/invoices/{tracking_id}").json()
    assert invoice["status"] == "received"
    assert invoice["revision"] == 2
    assert invoice["client_name"] == "Northwind"
    assert invoice["decision"] is None  # previous decision cleared while re-evaluating
    assert invoice["vendor"] == "PixelForge"  # immutable fields untouched
    assert invoice["amount"] == 300.0

    # republished with the new revision and a new event identity
    received_events = [event for topic, event in published if topic == "invoice.received"]
    assert received_events[-1]["revision"] == 2
    assert received_events[-1]["invoice"]["client_name"] == "Northwind"
    assert received_events[-1]["event_id"] != received_events[0]["event_id"]
    assert received_events[-1]["correlation_id"] == received_events[0]["correlation_id"]


def test_additional_info_requires_at_least_one_field(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    client.post(
        f"/approvals/{tracking_id}/actions",
        json=action_payload("request_more_info", comment="More info please"),
    )
    response = client.post(f"/invoices/{tracking_id}/additional-info", json={})
    assert response.status_code == 422


def test_stale_revision_decision_is_ignored(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    client.post(
        f"/approvals/{tracking_id}/actions",
        json=action_payload("request_more_info", comment="Need attendees"),
    )
    client.post(f"/invoices/{tracking_id}/additional-info", json={"attendees": 2})

    # a late decision from revision 1 must not touch the revision-2 invoice
    stale = client.post(
        "/events/invoice-decided", json=decided_event(tracking_id, status="rejected", revision=1)
    )
    assert stale.json() == {"status": "SUCCESS"}
    invoice = client.get(f"/invoices/{tracking_id}").json()
    assert invoice["status"] == "received"
    assert invoice["decision"] is None

    # the revision-2 decision applies
    client.post("/events/invoice-decided", json=decided_event(tracking_id, revision=2))
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "pending_review"


def test_duplicate_current_revision_decision_is_harmless(monkeypatch):
    tracking_id = submit_pending_review(monkeypatch)
    client.post(
        f"/approvals/{tracking_id}/actions",
        json=action_payload("request_more_info", comment="Need attendees"),
    )
    client.post(f"/invoices/{tracking_id}/additional-info", json={"attendees": 2})

    event = decided_event(tracking_id, revision=2)
    first = client.post("/events/invoice-decided", json=event)
    second = client.post("/events/invoice-decided", json=event)
    assert first.json() == {"status": "SUCCESS"}
    assert second.json() == {"status": "SUCCESS"}
    assert client.get(f"/invoices/{tracking_id}").json()["status"] == "pending_review"
