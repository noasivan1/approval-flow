"""Verification runner: starts the stack, waits for health, runs the journeys.

Invoked by `make verify` (or directly: python tests/verification/run.py).

The journeys assert deterministic routes, so the stack is temporarily brought
up with RECOMMENDATION_PROVIDER=stub and restored to the normal configuration
(gemini by default, or whatever .env sets) when the run finishes.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.getenv("VERIFY_BASE_URL", "http://localhost:8000")
HEALTH_TIMEOUT_SECONDS = 60
PIPELINE_TIMEOUT_SECONDS = 90

STUB_ENV = {**os.environ, "RECOMMENDATION_PROVIDER": "stub"}


def compose(*args, env=None):
    return subprocess.run(["docker", "compose", *args], capture_output=True, text=True, env=env)


def restore_runtime_provider() -> None:
    """Bring decision-service back to the normal (non-stub) configuration."""
    compose("up", "-d", "decision-service", env=os.environ)
    compose("up", "-d", "--force-recreate", "decision-service-dapr", env=os.environ)


def fail(message: str) -> None:
    print(f"\n{message}")
    print("VERIFICATION FAILED")
    sys.exit(1)


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def wait_for_pipeline() -> None:
    """Submit a probe invoice and wait until it is decided, so the journeys
    never race a freshly recreated decision pipeline."""
    payload = {
        "vendor": "Bistro 19",
        "amount": 1.0,
        "currency": "USD",
        "category": "meals",
        "attendees": 1,
        "receipt_present": True,
        "submitter_email": "verify-probe@northwind.example",
        "department": "engineering-2026Q2",
    }
    request = urllib.request.Request(
        f"{BASE_URL}/invoices",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        tracking_id = json.loads(response.read())["tracking_id"]

    deadline = time.monotonic() + PIPELINE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _get_json(f"{BASE_URL}/invoices/{tracking_id}")["status"] != "received":
            return
        time.sleep(3)
    fail(f"decision pipeline did not process the probe invoice within {PIPELINE_TIMEOUT_SECONDS}s.")


def main() -> None:
    up = compose("up", "-d", env=STUB_ENV)
    if up.returncode != 0:
        fail(
            "Could not start the Docker stack. Is Docker running?\n"
            + (up.stderr or up.stdout).strip()
        )
    # the sidecar shares the service's network namespace and must follow it
    compose("up", "-d", "--force-recreate", "decision-service-dapr", env=STUB_ENV)

    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while True:
            try:
                with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass
            if time.monotonic() > deadline:
                fail(f"API gateway did not become healthy at {BASE_URL} within {HEALTH_TIMEOUT_SECONDS}s.")
            time.sleep(2)

        wait_for_pipeline()

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/verification", "-q", "-ra", "-p", "no:cacheprovider"],
            env={**STUB_ENV, "VERIFY": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    finally:
        restore_runtime_provider()

    if result.returncode == 0:
        print("\nVERIFICATION PASSED")
        sys.exit(0)
    print("\nVERIFICATION FAILED")
    sys.exit(result.returncode or 1)


if __name__ == "__main__":
    main()
