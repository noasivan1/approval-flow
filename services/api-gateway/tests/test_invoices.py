from fastapi.testclient import TestClient

from app import workflow_client
from app.main import app

client = TestClient(app)

VALID_INVOICE = {
    "vendor": "Bistro 19",
    "amount": 42.0,
    "currency": "USD",
    "category": "meals",
    "receipt_present": True,
    "submitter_email": "dana.cohen@northwind.example",
    "note": "Solo working lunch.",
}


def test_submit_invoice_forwards_and_returns_tracking_id(monkeypatch):
    async def fake_submit(payload):
        assert payload["vendor"] == "Bistro 19"
        return {"tracking_id": "abc-123", "status": "received", **payload}

    monkeypatch.setattr(workflow_client, "submit_invoice", fake_submit)

    response = client.post("/invoices", json=VALID_INVOICE)
    assert response.status_code == 202
    body = response.json()
    assert body == {"tracking_id": "abc-123", "status": "received"}


def test_zero_amount_is_rejected_before_forwarding():
    response = client.post("/invoices", json={**VALID_INVOICE, "amount": 0})
    assert response.status_code == 422


def test_negative_amount_is_rejected_before_forwarding():
    response = client.post("/invoices", json={**VALID_INVOICE, "amount": -50})
    assert response.status_code == 422


def test_missing_required_field_is_rejected():
    payload = {key: value for key, value in VALID_INVOICE.items() if key != "submitter_email"}
    response = client.post("/invoices", json=payload)
    assert response.status_code == 422


def test_get_invoice_returns_workflow_response(monkeypatch):
    async def fake_get(tracking_id):
        return {"tracking_id": tracking_id, "status": "received", **VALID_INVOICE}

    monkeypatch.setattr(workflow_client, "get_invoice", fake_get)

    response = client.get("/invoices/abc-123")
    assert response.status_code == 200
    body = response.json()
    assert body["tracking_id"] == "abc-123"
    assert body["status"] == "received"


def test_unknown_tracking_id_returns_404(monkeypatch):
    async def fake_get(tracking_id):
        return None

    monkeypatch.setattr(workflow_client, "get_invoice", fake_get)

    response = client.get("/invoices/does-not-exist")
    assert response.status_code == 404
