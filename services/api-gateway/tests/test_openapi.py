import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_COMMITTED_SPEC = Path(__file__).resolve().parents[3] / "docs" / "openapi.json"

PUBLIC_PATHS = {
    "/health",
    "/invoices",
    "/invoices/{tracking_id}",
    "/invoices/{tracking_id}/trail",
    "/invoices/{tracking_id}/additional-info",
    "/approvals/queue",
    "/approvals/{tracking_id}/actions",
    "/notifications",
    "/dashboard",
}


def test_openapi_document_is_served():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "ApprovalFlow API"
    assert client.get("/docs").status_code == 200


def test_all_public_endpoints_are_documented():
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"].keys()) == PUBLIC_PATHS


def test_operations_have_summaries_and_typed_responses():
    spec = client.get("/openapi.json").json()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            assert operation.get("summary"), f"missing summary on {method.upper()} {path}"

    submit = spec["paths"]["/invoices"]["post"]
    assert submit["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SubmissionResult"
    }
    assert "429" in submit["responses"]

    action = spec["paths"]["/approvals/{tracking_id}/actions"]["post"]
    assert action["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReviewActionResult"
    }
    assert "409" in action["responses"]

    assert "404" in spec["paths"]["/invoices/{tracking_id}"]["get"]["responses"]


def test_committed_spec_matches_the_application():
    committed = json.loads(_COMMITTED_SPEC.read_text(encoding="utf-8"))
    live = client.get("/openapi.json").json()
    assert committed == live, (
        "docs/openapi.json is out of date; regenerate it with "
        "'python scripts/export_openapi.py'"
    )
