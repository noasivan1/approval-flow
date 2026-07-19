from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

VALID_INVOICE = {
    "vendor": "Bistro 19",
    "amount": 42.0,
    "currency": "USD",
    "category": "meals",
    "receipt_present": True,
    "submitter_email": "dana.cohen@northwind.example",
    "department": "engineering-2026Q2",
    "note": "Solo working lunch.",
}


def test_submit_invoice_returns_tracking_id_and_received_status():
    response = client.post("/invoices", json=VALID_INVOICE)
    assert response.status_code == 201
    body = response.json()
    assert body["tracking_id"]
    assert body["status"] == "received"
    assert body["vendor"] == "Bistro 19"
    assert body["created_at"]
    assert body["updated_at"]


def test_get_invoice_returns_created_invoice():
    created = client.post("/invoices", json=VALID_INVOICE).json()

    response = client.get(f"/invoices/{created['tracking_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["tracking_id"] == created["tracking_id"]
    assert body["status"] == "received"
    assert body["amount"] == 42.0
    assert body["submitter_email"] == "dana.cohen@northwind.example"


def test_zero_amount_is_rejected():
    response = client.post("/invoices", json={**VALID_INVOICE, "amount": 0})
    assert response.status_code == 422


def test_negative_amount_is_rejected():
    response = client.post("/invoices", json={**VALID_INVOICE, "amount": -10})
    assert response.status_code == 422


def test_missing_vendor_is_rejected():
    payload = {key: value for key, value in VALID_INVOICE.items() if key != "vendor"}
    response = client.post("/invoices", json=payload)
    assert response.status_code == 422


def test_missing_department_is_rejected():
    payload = {key: value for key, value in VALID_INVOICE.items() if key != "department"}
    response = client.post("/invoices", json=payload)
    assert response.status_code == 422


def test_client_vendor_known_is_ignored_and_not_stored():
    response = client.post("/invoices", json={**VALID_INVOICE, "vendor_known": True})
    assert response.status_code == 201
    lookup = client.get(f"/invoices/{response.json()['tracking_id']}").json()
    assert "vendor_known" not in lookup


def test_unknown_tracking_id_returns_404():
    response = client.get("/invoices/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
