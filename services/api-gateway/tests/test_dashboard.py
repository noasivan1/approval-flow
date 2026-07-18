from fastapi.testclient import TestClient

from app import workflow_client
from app.main import app

client = TestClient(app)


def test_dashboard_is_forwarded(monkeypatch):
    summary = {"total_invoices": 3, "paid_count": 1}

    async def fake_dashboard():
        return summary

    monkeypatch.setattr(workflow_client, "get_dashboard", fake_dashboard)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert response.json() == summary


def test_trail_is_forwarded(monkeypatch):
    trail = {"tracking_id": "abc", "decision": {"route": "auto_approve"}, "review_actions": []}

    async def fake_trail(tracking_id):
        return trail

    monkeypatch.setattr(workflow_client, "get_trail", fake_trail)
    response = client.get("/invoices/abc/trail")
    assert response.status_code == 200
    assert response.json() == trail


def test_trail_unknown_returns_404(monkeypatch):
    async def fake_trail(tracking_id):
        return None

    monkeypatch.setattr(workflow_client, "get_trail", fake_trail)
    assert client.get("/invoices/missing/trail").status_code == 404


def test_notifications_forwarded_with_email(monkeypatch):
    captured = {}

    async def fake_notifications(submitter_email):
        captured["email"] = submitter_email
        return [{"status": "paid", "message": "m"}]

    monkeypatch.setattr(workflow_client, "get_notifications", fake_notifications)
    response = client.get("/notifications", params={"submitter_email": "a@b.example"})
    assert response.status_code == 200
    assert captured["email"] == "a@b.example"
    assert response.json()[0]["status"] == "paid"


def test_notifications_require_email():
    assert client.get("/notifications").status_code == 422
