import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, ratelimit, workflow_client
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
    "vendor_known": True,
    "attendees": 1,
}


class FailingStore:
    async def increment(self, key, ttl_seconds):
        raise ratelimit.RateLimitUnavailable("store down")


def make_limiter(limit, clock=None):
    store = ratelimit.InMemoryCounterStore()
    if clock:
        return ratelimit.RateLimiter(store, limit, clock=clock)
    return ratelimit.RateLimiter(store, limit)


@pytest.fixture
def forwarded(monkeypatch):
    calls = []

    async def fake_submit(payload):
        calls.append(payload)
        return {"tracking_id": "t-1", "status": "received"}

    monkeypatch.setattr(workflow_client, "submit_invoice", fake_submit)
    return calls


def test_below_limit_passes_then_429(monkeypatch, forwarded):
    monkeypatch.setattr(main, "limiter", make_limiter(2))

    first = client.post("/invoices", json=VALID_INVOICE)
    second = client.post("/invoices", json=VALID_INVOICE)
    third = client.post("/invoices", json=VALID_INVOICE)

    assert first.status_code == 202
    assert second.status_code == 202
    assert third.status_code == 429
    assert third.json() == {"detail": "rate limit exceeded"}
    assert "Retry-After" in third.headers
    assert 1 <= int(third.headers["Retry-After"]) <= 60
    assert third.headers.get("X-Correlation-ID")
    # the blocked request never reached workflow-service
    assert len(forwarded) == 2


def test_clients_are_isolated():
    limiter = make_limiter(1)
    allowed_a1, _ = asyncio.run(limiter.check("client-a", "submissions"))
    allowed_a2, _ = asyncio.run(limiter.check("client-a", "submissions"))
    allowed_b1, _ = asyncio.run(limiter.check("client-b", "submissions"))
    assert allowed_a1 is True
    assert allowed_a2 is False
    assert allowed_b1 is True


def test_route_groups_are_isolated():
    limiter = make_limiter(1)
    assert asyncio.run(limiter.check("c", "submissions"))[0] is True
    assert asyncio.run(limiter.check("c", "submissions"))[0] is False
    # a different group still has its own allowance
    assert asyncio.run(limiter.check("c", "approvals"))[0] is True
    assert asyncio.run(limiter.check("c", "additional-info"))[0] is True


def test_new_window_resets_allowance():
    now = {"value": 1000.0}
    limiter = make_limiter(1, clock=lambda: now["value"])
    assert asyncio.run(limiter.check("c", "submissions"))[0] is True
    assert asyncio.run(limiter.check("c", "submissions"))[0] is False
    now["value"] += ratelimit.WINDOW_SECONDS
    assert asyncio.run(limiter.check("c", "submissions"))[0] is True


def test_limit_zero_disables(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "0")
    assert ratelimit.build_limiter() is None


def test_gets_and_infrastructure_routes_are_not_limited(monkeypatch):
    monkeypatch.setattr(main, "limiter", make_limiter(0))  # any protected POST would block

    async def fake_get(tracking_id):
        return {"tracking_id": tracking_id, "status": "received"}

    async def fake_queue():
        return []

    async def fake_dashboard():
        return {"total_invoices": 0}

    async def fake_trail(tracking_id):
        return {"tracking_id": tracking_id}

    async def fake_notifications(submitter_email):
        return []

    monkeypatch.setattr(workflow_client, "get_invoice", fake_get)
    monkeypatch.setattr(workflow_client, "get_approvals_queue", fake_queue)
    monkeypatch.setattr(workflow_client, "get_dashboard", fake_dashboard)
    monkeypatch.setattr(workflow_client, "get_trail", fake_trail)
    monkeypatch.setattr(workflow_client, "get_notifications", fake_notifications)

    assert client.get("/invoices/abc").status_code == 200
    assert client.get("/approvals/queue").status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.get("/invoices/abc/trail").status_code == 200
    assert client.get("/notifications", params={"submitter_email": "a@b.c"}).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_unavailable_backend_returns_503_for_mutations(monkeypatch, forwarded):
    monkeypatch.setattr(main, "limiter", ratelimit.RateLimiter(FailingStore(), 60))

    response = client.post("/invoices", json=VALID_INVOICE)
    assert response.status_code == 503
    assert response.json() == {"detail": "rate limiting unavailable"}
    assert response.headers.get("X-Correlation-ID")
    assert forwarded == []  # not silently bypassed

    # reads still work when the counter store is down
    async def fake_dashboard():
        return {"total_invoices": 0}

    monkeypatch.setattr(workflow_client, "get_dashboard", fake_dashboard)
    assert client.get("/dashboard").status_code == 200


def test_route_group_matching():
    assert ratelimit.route_group("POST", "/invoices") == "submissions"
    assert ratelimit.route_group("POST", "/approvals/abc/actions") == "approvals"
    assert ratelimit.route_group("POST", "/invoices/abc/additional-info") == "additional-info"
    assert ratelimit.route_group("GET", "/invoices") is None
    assert ratelimit.route_group("GET", "/invoices/abc") is None
    assert ratelimit.route_group("GET", "/dashboard") is None
    assert ratelimit.route_group("GET", "/health") is None
    assert ratelimit.route_group("POST", "/events/anything") is None


def test_dapr_store_request_shape():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(204)
        return httpx.Response(204)

    store = ratelimit.DaprStateCounterStore(transport=httpx.MockTransport(handler))
    value = asyncio.run(store.increment("rate-limit:1.2.3.4:submissions:99", ttl_seconds=120))
    assert value == 1

    get_request, post_request = requests
    assert get_request.url.path.endswith("/v1.0/state/statestore/rate-limit:1.2.3.4:submissions:99")
    body = json.loads(post_request.content)
    assert body[0]["key"] == "rate-limit:1.2.3.4:submissions:99"
    assert body[0]["value"] == 1
    assert body[0]["metadata"]["ttlInSeconds"] == "120"
    assert body[0]["options"]["concurrency"] == "first-write"
    assert "etag" not in body[0]


def test_dapr_store_uses_etag_and_retries_conflicts():
    state = {"reads": 0, "writes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            state["reads"] += 1
            # first read: counter at 3; after the conflict: counter at 4
            value = 3 if state["reads"] == 1 else 4
            return httpx.Response(200, json=value, headers={"etag": str(value)})
        state["writes"] += 1
        body = json.loads(request.content)
        if state["writes"] == 1:
            assert body[0]["etag"] == "3"
            return httpx.Response(409)  # lost the race
        assert body[0]["etag"] == "4"
        assert body[0]["value"] == 5
        return httpx.Response(204)

    store = ratelimit.DaprStateCounterStore(transport=httpx.MockTransport(handler))
    value = asyncio.run(store.increment("rate-limit:c:submissions:1", ttl_seconds=120))
    assert value == 5
    assert state["writes"] == 2


def test_dapr_store_unreachable_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sidecar down")

    store = ratelimit.DaprStateCounterStore(transport=httpx.MockTransport(handler))
    with pytest.raises(ratelimit.RateLimitUnavailable):
        asyncio.run(store.increment("k", ttl_seconds=120))


def test_dapr_store_exhausted_retries_raise_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=1, headers={"etag": "1"})
        return httpx.Response(409)  # always conflicting

    store = ratelimit.DaprStateCounterStore(transport=httpx.MockTransport(handler))
    with pytest.raises(ratelimit.RateLimitUnavailable):
        asyncio.run(store.increment("k", ttl_seconds=120))
