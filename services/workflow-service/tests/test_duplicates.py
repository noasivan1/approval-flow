import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app import events, main
from app.main import app
from app.repository import duplicate_key_for, get_repository

client = TestClient(app)

# mirrors INV-1001 from data/sample-invoices.json
INV_1001 = {
    "vendor": "Bistro 19",
    "amount": 42.0,
    "currency": "USD",
    "category": "meals",
    "receipt_present": True,
    "submitter_email": "dana.cohen@northwind.example",
    "vendor_known": True,
    "attendees": 1,
    "invoice_number": "NW-INV-7781",
}


def fresh_payload(**overrides):
    payload = {**INV_1001, "invoice_number": f"NW-{uuid.uuid4().hex[:10]}"}
    payload.update(overrides)
    return payload


def patch_publisher(monkeypatch):
    published = []

    async def fake_publish(topic, data):
        published.append((topic, data))

    monkeypatch.setattr(events, "publish", fake_publish)
    return published


def decided_event(tracking_id, status="pending_review", revision=1):
    route = {"approved": "auto_approve", "pending_review": "human_review", "rejected": "reject"}[status]
    return {
        "data": {
            "event_id": str(uuid.uuid4()),
            "tracking_id": tracking_id,
            "correlation_id": "corr-dup",
            "revision": revision,
            "route": route,
            "status": status,
            "recommendation": "review",
            "confidence": 0.95,
            "rule_ids": ["AUTONOMY-CEILING"],
            "reasons": [],
            "plain_language_reason": "Escalated for human review.",
            "amount_usd": 42.0,
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        }
    }


def test_normalization_of_key_fields():
    base = duplicate_key_for(INV_1001)
    assert base == "bistro 19|NW-INV-7781|USD|42.00"
    assert duplicate_key_for({**INV_1001, "vendor": "  BISTRO 19 "}) == base
    assert duplicate_key_for({**INV_1001, "invoice_number": " nw-inv-7781 "}) == base
    assert duplicate_key_for({**INV_1001, "currency": " usd "}) == base
    assert duplicate_key_for({**INV_1001, "amount": 42}) == base
    assert duplicate_key_for({**INV_1001, "amount": 42.00}) == base


def test_missing_invoice_number_has_no_key():
    assert duplicate_key_for({**INV_1001, "invoice_number": None}) is None
    assert duplicate_key_for({**INV_1001, "invoice_number": "   "}) is None


def test_first_submission_is_created_and_published(monkeypatch):
    published = patch_publisher(monkeypatch)
    response = client.post("/invoices", json=fresh_payload())
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "received"
    assert "duplicate_of" not in body
    assert "duplicate_key" not in body
    assert len(published) == 1


def test_second_identical_submission_becomes_duplicate(monkeypatch):
    published = patch_publisher(monkeypatch)
    payload = fresh_payload()
    original = client.post("/invoices", json=payload).json()
    # process the original like INV-1001 before INV-1007 arrives
    client.post("/events/invoice-decided", json=decided_event(original["tracking_id"], status="approved"))

    duplicate = client.post("/invoices", json=payload)
    assert duplicate.status_code == 201
    body = duplicate.json()
    assert body["status"] == "duplicate"
    assert body["duplicate_of"] == original["tracking_id"]
    assert body["tracking_id"] != original["tracking_id"]

    # only the original published invoice.received
    received = [e for topic, e in published if topic == "invoice.received"]
    assert len(received) == 1
    assert received[0]["tracking_id"] == original["tracking_id"]


def test_duplicate_lookup_returns_link_and_synthetic_decision(monkeypatch):
    patch_publisher(monkeypatch)
    payload = fresh_payload()
    original = client.post("/invoices", json=payload).json()
    duplicate = client.post("/invoices", json=payload).json()

    lookup = client.get(f"/invoices/{duplicate['tracking_id']}").json()
    assert lookup["status"] == "duplicate"
    assert lookup["duplicate_of"] == original["tracking_id"]
    decision = lookup["decision"]
    assert decision["route"] == "duplicate"
    assert decision["rule_ids"] == ["GLOBAL-DUP"]
    assert decision["reasons"][0]["code"] == "duplicate_invoice"
    assert original["tracking_id"] in decision["reasons"][0]["detail"]
    assert decision["plain_language_reason"]
    assert decision["evaluated_at"]
    assert "recommendation" not in decision
    assert "confidence" not in decision


def test_case_and_whitespace_variants_are_still_duplicates(monkeypatch):
    patch_publisher(monkeypatch)
    payload = fresh_payload()
    client.post("/invoices", json=payload)
    variant = {
        **payload,
        "vendor": f"  {payload['vendor'].upper()}  ",
        "invoice_number": f" {payload['invoice_number'].lower()} ",
        "amount": int(payload["amount"]) if payload["amount"] == int(payload["amount"]) else payload["amount"],
    }
    response = client.post("/invoices", json=variant)
    assert response.json()["status"] == "duplicate"


def test_different_identity_fields_are_not_duplicates(monkeypatch):
    patch_publisher(monkeypatch)
    payload = fresh_payload()
    client.post("/invoices", json=payload)

    other_vendor = client.post("/invoices", json={**payload, "vendor": "Trattoria Verde"})
    assert other_vendor.json()["status"] == "received"

    other_number = client.post("/invoices", json={**payload, "invoice_number": f"NW-{uuid.uuid4().hex[:8]}"})
    assert other_number.json()["status"] == "received"

    other_currency = client.post("/invoices", json={**payload, "currency": "EUR"})
    assert other_currency.json()["status"] == "received"


def test_missing_invoice_number_twice_is_two_originals(monkeypatch):
    patch_publisher(monkeypatch)
    payload = fresh_payload(invoice_number=None)
    first = client.post("/invoices", json=payload).json()
    second = client.post("/invoices", json=payload).json()
    assert first["status"] == "received"
    assert second["status"] == "received"
    assert first["tracking_id"] != second["tracking_id"]


def test_duplicate_never_appears_in_approver_queue(monkeypatch):
    patch_publisher(monkeypatch)
    payload = fresh_payload()
    original = client.post("/invoices", json=payload).json()
    client.post("/events/invoice-decided", json=decided_event(original["tracking_id"]))
    duplicate = client.post("/invoices", json=payload).json()

    ids = [item["tracking_id"] for item in client.get("/approvals/queue").json()]
    assert original["tracking_id"] in ids
    assert duplicate["tracking_id"] not in ids


def test_concurrent_identical_submissions_one_original(monkeypatch):
    patch_publisher(monkeypatch)
    payload = fresh_payload()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post("/invoices", json=payload).json(), range(2)))

    statuses = sorted(r["status"] for r in results)
    assert statuses == ["duplicate", "received"]
    original = next(r for r in results if r["status"] == "received")
    duplicate = next(r for r in results if r["status"] == "duplicate")
    assert duplicate["duplicate_of"] == original["tracking_id"]


def test_sweep_includes_unpublished_original_and_excludes_duplicates(monkeypatch):
    async def failing_publish(topic, data):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(events, "publish", failing_publish)
    payload = fresh_payload()
    original = client.post("/invoices", json=payload).json()
    duplicate = client.post("/invoices", json=payload).json()
    assert duplicate["status"] == "duplicate"

    monkeypatch.setattr(main, "_SWEEP_GRACE_SECONDS", -5)
    pending = get_repository().find_unpublished("9999-12-31T00:00:00+00:00")
    pending_ids = [inv["tracking_id"] for inv in pending]
    assert original["tracking_id"] in pending_ids
    assert duplicate["tracking_id"] not in pending_ids


def test_request_more_info_reevaluation_is_not_a_duplicate(monkeypatch):
    published = patch_publisher(monkeypatch)
    payload = fresh_payload()
    original = client.post("/invoices", json=payload).json()
    client.post("/events/invoice-decided", json=decided_event(original["tracking_id"]))
    client.post(
        f"/approvals/{original['tracking_id']}/actions",
        json={
            "action": "request_more_info",
            "reviewer_email": "finance@northwind.example",
            "comment": "Need attendee count",
            "idempotency_key": str(uuid.uuid4()),
        },
    )

    response = client.post(f"/invoices/{original['tracking_id']}/additional-info", json={"attendees": 2})
    assert response.json() == {"tracking_id": original["tracking_id"], "status": "received", "revision": 2}

    invoice = client.get(f"/invoices/{original['tracking_id']}").json()
    assert invoice["status"] == "received"
    assert invoice["revision"] == 2
    assert "duplicate_of" not in invoice
    # re-evaluation republished the same tracking_id, not a new record
    received = [e for topic, e in published if topic == "invoice.received"]
    assert all(e["tracking_id"] == original["tracking_id"] for e in received)


def test_additional_info_with_new_noncolliding_number_continues(monkeypatch):
    patch_publisher(monkeypatch)
    payload = fresh_payload(invoice_number=None)
    original = client.post("/invoices", json=payload).json()
    client.post("/events/invoice-decided", json=decided_event(original["tracking_id"]))
    client.post(
        f"/approvals/{original['tracking_id']}/actions",
        json={
            "action": "request_more_info",
            "reviewer_email": "finance@northwind.example",
            "comment": "Please add the invoice number",
            "idempotency_key": str(uuid.uuid4()),
        },
    )

    new_number = f"NW-{uuid.uuid4().hex[:8]}"
    response = client.post(
        f"/invoices/{original['tracking_id']}/additional-info", json={"invoice_number": new_number}
    )
    assert response.json()["status"] == "received"

    # the key is now owned by this invoice: a new submission with it is a duplicate
    resubmission = client.post("/invoices", json={**payload, "invoice_number": new_number}).json()
    assert resubmission["status"] == "duplicate"
    assert resubmission["duplicate_of"] == original["tracking_id"]


def test_additional_info_with_colliding_number_becomes_duplicate(monkeypatch):
    published = patch_publisher(monkeypatch)
    taken = fresh_payload()
    original = client.post("/invoices", json=taken).json()

    unnumbered = fresh_payload(invoice_number=None)
    pending = client.post("/invoices", json=unnumbered).json()
    client.post("/events/invoice-decided", json=decided_event(pending["tracking_id"]))
    client.post(
        f"/approvals/{pending['tracking_id']}/actions",
        json={
            "action": "request_more_info",
            "reviewer_email": "finance@northwind.example",
            "comment": "Please add the invoice number",
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    events_before = len(published)

    response = client.post(
        f"/invoices/{pending['tracking_id']}/additional-info",
        json={"invoice_number": taken["invoice_number"]},
    )
    assert response.status_code == 200
    assert response.json() == {
        "tracking_id": pending["tracking_id"],
        "status": "duplicate",
        "duplicate_of": original["tracking_id"],
    }

    # no re-evaluation event was published for the confirmed duplicate
    assert len(published) == events_before

    invoice = client.get(f"/invoices/{pending['tracking_id']}").json()
    assert invoice["status"] == "duplicate"
    assert invoice["duplicate_of"] == original["tracking_id"]
    assert invoice["decision"]["rule_ids"] == ["GLOBAL-DUP"]
    # review history is preserved
    assert len(invoice["review_actions"]) == 1
    assert invoice["review_actions"][0]["action"] == "request_more_info"
