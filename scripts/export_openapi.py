"""Regenerates docs/openapi.json from the API gateway.

Run after changing any public endpoint:

    python scripts/export_openapi.py

A gateway test compares the committed file with the live application, so CI
fails when the two drift apart.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "api-gateway"))

from app.main import app  # noqa: E402

spec = app.openapi()
target = ROOT / "docs" / "openapi.json"
target.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
print(f"wrote {target} ({len(spec['paths'])} paths)")
