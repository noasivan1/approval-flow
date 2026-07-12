from fastapi.testclient import TestClient

from app import workflow_client
from app.main import app

client = TestClient(app)

ACTION = {
    "action": "approve",
    "reviewer_email": "finance@northwind.example",
    "comment": None,
    "idempotency_key": "key-1",
}


def test_queue_is_forwarded(monkeypatch):
    queue = [{"tracking_id": "abc", "vendor": "PixelForge", "status": "pending_review"}]

    async def fake_queue():
        return queue

    monkeypatch.setattr(workflow_client, "get_approvals_queue", fake_queue)
    response = client.get("/approvals/queue")
    assert response.status_code == 200
    assert response.json() == queue


def test_review_action_forwards_payload(monkeypatch):
    captured = {}

    async def fake_action(tracking_id, payload):
        captured["tracking_id"] = tracking_id
        captured["payload"] = payload
        return 200, {"tracking_id": tracking_id, "status": "approved", "result": "applied"}

    monkeypatch.setattr(workflow_client, "submit_review_action", fake_action)
    response = client.post("/approvals/abc-123/actions", json=ACTION)
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert captured["tracking_id"] == "abc-123"
    assert captured["payload"]["idempotency_key"] == "key-1"


def test_review_conflict_passes_through(monkeypatch):
    async def fake_action(tracking_id, payload):
        return 409, {"detail": "Invoice is no longer awaiting review"}

    monkeypatch.setattr(workflow_client, "submit_review_action", fake_action)
    response = client.post("/approvals/abc-123/actions", json=ACTION)
    assert response.status_code == 409
    assert response.json()["detail"] == "Invoice is no longer awaiting review"


def test_review_unknown_invoice_passes_through(monkeypatch):
    async def fake_action(tracking_id, payload):
        return 404, {"detail": "Invoice not found"}

    monkeypatch.setattr(workflow_client, "submit_review_action", fake_action)
    response = client.post("/approvals/abc-123/actions", json=ACTION)
    assert response.status_code == 404


def test_request_more_info_without_comment_rejected_locally():
    response = client.post(
        "/approvals/abc-123/actions",
        json={**ACTION, "action": "request_more_info"},
    )
    assert response.status_code == 422


def test_additional_info_forwards_fields(monkeypatch):
    captured = {}

    async def fake_info(tracking_id, payload):
        captured.update(payload)
        return 200, {"tracking_id": tracking_id, "status": "received", "revision": 2}

    monkeypatch.setattr(workflow_client, "submit_additional_info", fake_info)
    response = client.post(
        "/invoices/abc-123/additional-info",
        json={"attendees": 4, "client_name": "Northwind"},
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert captured["attendees"] == 4
    assert captured["client_name"] == "Northwind"


def test_additional_info_requires_a_field():
    response = client.post("/invoices/abc-123/additional-info", json={})
    assert response.status_code == 422
