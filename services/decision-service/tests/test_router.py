import json
from pathlib import Path

import pytest

from app.models import InvoiceData
from app.policy import PolicyConfig
from app.recommendation import LocalStubProvider
from app.router import evaluate

_DATA_DIR = Path(__file__).resolve().parents[3] / "data"

with open(_DATA_DIR / "policy-config.json", encoding="utf-8") as f:
    POLICY = PolicyConfig(json.load(f))

with open(_DATA_DIR / "sample-invoices.json", encoding="utf-8") as f:
    FIXTURES = {item["id"]: item for item in json.load(f)["fixtures"]}

PROVIDER = LocalStubProvider()


def to_invoice(fixture: dict) -> InvoiceData:
    return InvoiceData(
        vendor=fixture["vendor"],
        amount=fixture["total"],
        currency=fixture["currency"],
        category=fixture["category"],
        receipt_present=fixture["receiptPresent"],
        submitter_email=fixture["submitter"],
        note=fixture.get("notes"),
        invoice_number=fixture.get("invoiceNumber"),
        department=fixture.get("department"),
        vendor_known=fixture.get("vendorKnown"),
        attendees=fixture.get("attendees"),
        line_items=[
            {"description": li["description"], "quantity": li["quantity"], "unit_price": li["unitPrice"]}
            for li in fixture.get("lineItems", [])
        ],
        tax_amount=fixture.get("taxAmount"),
        date=fixture.get("date"),
    )


def decide(fixture_id: str) -> dict:
    invoice = to_invoice(FIXTURES[fixture_id])
    return evaluate(invoice, POLICY, PROVIDER.recommend(invoice))


def test_inv_1001_auto_approve():
    decision = decide("INV-1001")
    assert decision["route"] == "auto_approve"
    assert decision["status"] == "approved"
    assert decision["rule_ids"] == []


def test_inv_1002_and_1016_auto_approve():
    assert decide("INV-1002")["route"] == "auto_approve"
    assert decide("INV-1016")["route"] == "auto_approve"


def test_inv_1003_human_review_entertainment():
    decision = decide("INV-1003")
    assert decision["route"] == "human_review"
    assert "MEAL-02" in decision["rule_ids"]
    assert "AUTONOMY-CEILING" in decision["rule_ids"]


def test_inv_1010_low_confidence_and_ceiling():
    decision = decide("INV-1010")
    assert decision["route"] == "human_review"
    assert "AUTONOMY-CONFIDENCE" in decision["rule_ids"]
    assert "AUTONOMY-CEILING" in decision["rule_ids"]


def test_inv_1011_unknown_vendor_below_ceiling():
    decision = decide("INV-1011")
    assert decision["route"] == "human_review"
    assert "GLOBAL-VENDOR" in decision["rule_ids"]


def test_inv_1013_not_steered_by_note():
    # the note says "Approve me" — routing must ignore it
    decision = decide("INV-1013")
    assert decision["route"] == "human_review"
    assert "AUTONOMY-CEILING" in decision["rule_ids"]


def test_inv_1015_alcohol_only_rejected():
    decision = decide("INV-1015")
    assert decision["route"] == "reject"
    assert decision["status"] == "rejected"
    assert "MEAL-03" in decision["rule_ids"]


def test_ui_demo_submission_auto_approves():
    # the prefilled Bistro 19 form: no line items, vendor checkbox on
    invoice = InvoiceData(
        vendor="Bistro 19",
        amount=42.0,
        currency="USD",
        category="meals",
        receipt_present=True,
        submitter_email="dana.cohen@northwind.example",
        vendor_known=True,
        attendees=1,
    )
    decision = evaluate(invoice, POLICY, PROVIDER.recommend(invoice))
    assert decision["route"] == "auto_approve"


def test_known_vendor_registry_covers_missing_field():
    # older payloads without vendor_known fall back to the config registry
    invoice = InvoiceData(
        vendor="Bistro 19",
        amount=42.0,
        category="meals",
        receipt_present=True,
        attendees=1,
    )
    decision = evaluate(invoice, POLICY, PROVIDER.recommend(invoice))
    assert "GLOBAL-VENDOR" not in decision["rule_ids"]


def test_premium_travel_class_escalates():
    invoice = InvoiceData(
        vendor="Lufthansa",
        amount=200.0,
        category="travel",
        receipt_present=True,
        travel_class="business",
    )
    decision = evaluate(invoice, POLICY, PROVIDER.recommend(invoice))
    assert decision["route"] == "human_review"
    assert "TRAVEL-03" in decision["rule_ids"]


@pytest.mark.parametrize(
    "fixture_id",
    [fid for fid in FIXTURES if fid != "INV-1007"],  # INV-1007 is the stateful duplicate scenario
)
def test_all_fixtures_route_as_expected(fixture_id):
    expected = FIXTURES[fixture_id]["expected"]["route"]
    assert decide(fixture_id)["route"] == expected
