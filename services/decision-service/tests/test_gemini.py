import json

import httpx
import pytest

from app.gemini import GeminiRecommendationProvider
from app.models import InvoiceData
from app.recommendation import LocalStubProvider, UnsupportedProvider, get_provider

INVOICE = InvoiceData(
    vendor="Bistro 19",
    amount=42.0,
    currency="USD",
    category="meals",
    receipt_present=True,
    attendees=1,
    note="Solo working lunch.",
)


def gemini_reply(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]},
    )


def make_provider(handler) -> GeminiRecommendationProvider:
    return GeminiRecommendationProvider(transport=httpx.MockTransport(handler))


def secret_response(key_value: str) -> httpx.Response:
    return httpx.Response(200, json={"GEMINI_API_KEY": key_value})


def assert_provider_error(result, category):
    assert result.suggestion == "review"
    assert result.confidence == 0.0
    assert result.provider == "gemini"
    assert result.provider_error_category == category
    assert category in result.rationale


def test_factory_selection(monkeypatch):
    monkeypatch.setenv("RECOMMENDATION_PROVIDER", "gemini")
    assert isinstance(get_provider(), GeminiRecommendationProvider)

    monkeypatch.setenv("RECOMMENDATION_PROVIDER", "stub")
    provider = get_provider()
    assert isinstance(provider, LocalStubProvider)
    result = provider.recommend(INVOICE)
    assert result.provider == "stub"
    assert result.provider_error_category is None

    monkeypatch.setenv("RECOMMENDATION_PROVIDER", "bogus")
    misconfigured = get_provider()
    assert isinstance(misconfigured, UnsupportedProvider)
    outcome = misconfigured.recommend(INVOICE)
    assert outcome.suggestion == "review"
    assert outcome.confidence == 0.0
    assert outcome.provider_error_category == "unsupported_provider"


def test_successful_structured_response(monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "localhost":
            return secret_response("test-key-123")
        return gemini_reply({"recommendation": "approve", "confidence": 0.91, "rationale": "clean invoice"})

    result = make_provider(handler).recommend(INVOICE)

    assert result.suggestion == "approve"
    assert result.confidence == 0.91
    assert result.provider == "gemini"
    assert result.model == "gemini-2.0-flash"
    assert result.provider_error_category is None

    secret_request, gemini_request = requests
    assert "/v1.0/secrets/secretstore/GEMINI_API_KEY" in str(secret_request.url)
    assert gemini_request.headers["x-goog-api-key"] == "test-key-123"
    assert "test-key-123" not in str(gemini_request.url)
    body = json.loads(gemini_request.content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "Bistro 19" in body["contents"][0]["parts"][0]["text"]


def test_missing_secret_yields_provider_error_not_stub(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-must-be-ignored")
    gemini_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return httpx.Response(500)  # secret store unavailable
        gemini_calls.append(request)
        return gemini_reply({"recommendation": "approve", "confidence": 0.9})

    result = make_provider(handler).recommend(INVOICE)

    assert_provider_error(result, "missing_secret")
    assert gemini_calls == []  # no key, no call — and no env-var fallback either
    assert result != LocalStubProvider().recommend(INVOICE)
    assert "env-key-must-be-ignored" not in result.rationale


def test_empty_secret_value_is_missing_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return secret_response("")
        raise AssertionError("gemini must not be called without a key")

    assert_provider_error(make_provider(handler).recommend(INVOICE), "missing_secret")


def test_authentication_error_is_not_retried():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return secret_response("bad-key")
        attempts.append(request)
        return httpx.Response(400, json={"error": {"message": "API key not valid"}})

    result = make_provider(handler).recommend(INVOICE)
    assert_provider_error(result, "authentication_error")
    assert len(attempts) == 1
    assert "bad-key" not in result.rationale


def test_timeout_respects_bounded_retries(monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "2")
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return secret_response("k")
        attempts.append(request)
        raise httpx.ConnectTimeout("timed out")

    result = make_provider(handler).recommend(INVOICE)
    assert_provider_error(result, "timeout")
    assert len(attempts) == 3  # initial try + 2 retries


def test_rate_limit_retries_then_reports_category(monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "1")
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return secret_response("k")
        attempts.append(request)
        return httpx.Response(429)

    result = make_provider(handler).recommend(INVOICE)
    assert_provider_error(result, "rate_limited")
    assert len(attempts) == 2


def test_server_error_retry_then_success(monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_RETRIES", "2")
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return secret_response("k")
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(500)
        return gemini_reply({"recommendation": "approve", "confidence": 0.88, "rationale": "ok"})

    result = make_provider(handler).recommend(INVOICE)
    assert result.suggestion == "approve"
    assert result.provider_error_category is None
    assert len(attempts) == 2


def test_malformed_output_yields_invalid_provider_response():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return secret_response("k")
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "not json at all"}]}}]}
        )

    assert_provider_error(make_provider(handler).recommend(INVOICE), "invalid_provider_response")


def test_invalid_confidence_yields_invalid_provider_response():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return secret_response("k")
        return gemini_reply({"recommendation": "maybe", "confidence": 5.0})

    assert_provider_error(make_provider(handler).recommend(INVOICE), "invalid_provider_response")


def test_model_override(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom-model")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "localhost":
            return secret_response("k")
        seen["path"] = request.url.path
        return gemini_reply({"recommendation": "approve", "confidence": 0.9})

    result = make_provider(handler).recommend(INVOICE)
    assert seen["path"] == "/v1beta/models/gemini-custom-model:generateContent"
    assert result.model == "gemini-custom-model"
