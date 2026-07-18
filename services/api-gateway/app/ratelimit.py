import os
import time

import httpx

WINDOW_SECONDS = 60

_GROUPS = {
    "submissions": lambda path: path == "/invoices",
    "approvals": lambda path: path.startswith("/approvals/") and path.endswith("/actions"),
    "additional-info": lambda path: path.startswith("/invoices/") and path.endswith("/additional-info"),
}


def route_group(method: str, path: str) -> str | None:
    """Only public state-changing routes are limited; everything else passes."""
    if method != "POST":
        return None
    for group, matches in _GROUPS.items():
        if matches(path):
            return group
    return None


class RateLimitUnavailable(Exception):
    pass


class InMemoryCounterStore:
    """Counter store for unit tests and bare local runs."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    async def increment(self, key: str, ttl_seconds: int) -> int:
        if len(self._counters) > 10000:  # old windows are never read again
            self._counters.clear()
        value = self._counters.get(key, 0) + 1
        self._counters[key] = value
        return value


class DaprStateCounterStore:
    """Counters in the Dapr state store (Redis), via the gateway's own sidecar.

    Uses first-write concurrency with the returned ETag and a small bounded
    retry so concurrent increments are not silently lost.
    """

    MAX_ATTEMPTS = 4

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        port = os.getenv("DAPR_HTTP_PORT", "3500")
        self._base = f"http://localhost:{port}/v1.0/state/statestore"
        self._transport = transport

    async def increment(self, key: str, ttl_seconds: int) -> int:
        try:
            async with httpx.AsyncClient(timeout=3.0, transport=self._transport) as client:
                for _ in range(self.MAX_ATTEMPTS):
                    current, etag = await self._read(client, key)
                    value = current + 1
                    item = {
                        "key": key,
                        "value": value,
                        "options": {"concurrency": "first-write"},
                        "metadata": {"ttlInSeconds": str(ttl_seconds)},
                    }
                    if etag:
                        item["etag"] = etag
                    response = await client.post(self._base, json=[item])
                    if response.status_code in (200, 204):
                        return value
                    if response.status_code not in (409, 500):
                        response.raise_for_status()
                    # 409/500: another request won the write; re-read and retry
        except httpx.HTTPError as exc:
            raise RateLimitUnavailable(f"state store unreachable: {exc}") from exc
        raise RateLimitUnavailable("concurrent counter updates exhausted retries")

    async def _read(self, client: httpx.AsyncClient, key: str) -> tuple[int, str | None]:
        response = await client.get(f"{self._base}/{key}")
        if response.status_code == 204 or not response.content:
            return 0, None
        response.raise_for_status()
        return int(response.json()), response.headers.get("etag")


class RateLimiter:
    def __init__(self, store, limit_per_minute: int, clock=time.time) -> None:
        self._store = store
        self._limit = limit_per_minute
        self._clock = clock

    async def check(self, client_id: str, group: str) -> tuple[bool, int]:
        """Returns (allowed, seconds until the current window ends)."""
        now = self._clock()
        window_id = int(now // WINDOW_SECONDS)
        key = f"rate-limit:{client_id}:{group}:{window_id}"
        count = await self._store.increment(key, ttl_seconds=WINDOW_SECONDS * 2)
        retry_after = WINDOW_SECONDS - int(now % WINDOW_SECONDS)
        return count <= self._limit, max(retry_after, 1)


def build_limiter() -> RateLimiter | None:
    limit = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    if limit <= 0:
        return None
    backend = os.getenv("RATE_LIMIT_BACKEND", "memory")
    store = DaprStateCounterStore() if backend == "dapr" else InMemoryCounterStore()
    return RateLimiter(store, limit)
