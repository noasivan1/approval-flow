"""Verification runner: starts the stack, waits for health, runs the journeys.

Invoked by `make verify` (or directly: python tests/verification/run.py).
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.getenv("VERIFY_BASE_URL", "http://localhost:8000")
HEALTH_TIMEOUT_SECONDS = 60


def fail(message: str) -> None:
    print(f"\n{message}")
    print("VERIFICATION FAILED")
    sys.exit(1)


def main() -> None:
    up = subprocess.run(["docker", "compose", "up", "-d"], capture_output=True, text=True)
    if up.returncode != 0:
        fail(
            "Could not start the Docker stack. Is Docker running?\n"
            + (up.stderr or up.stdout).strip()
        )

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

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/verification", "-q", "-ra", "-p", "no:cacheprovider"],
        env={**os.environ, "VERIFY": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode == 0:
        print("\nVERIFICATION PASSED")
        sys.exit(0)
    print("\nVERIFICATION FAILED")
    sys.exit(result.returncode or 1)


if __name__ == "__main__":
    main()
