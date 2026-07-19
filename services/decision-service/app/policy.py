import json
import os
import unicodedata
from pathlib import Path


def normalize_vendor(name: str) -> str:
    """Canonical form for vendor identity: NFKC, trimmed, single spaces, casefolded."""
    collapsed = " ".join(unicodedata.normalize("NFKC", name).split())
    return collapsed.casefold()


class PolicyConfig:
    def __init__(self, raw: dict) -> None:
        self.autonomy_ceiling_usd = float(raw["autonomy_ceiling_usd"])
        self.autonomy_confidence = float(raw["autonomy_confidence"])
        self.receipt_required_over_usd = float(raw["receipt_required_over_usd"])
        self.meal_per_attendee_usd = float(raw["meal_per_attendee_usd"])
        self.entertainment_justification_over_usd = float(raw["entertainment_justification_over_usd"])
        self.saas_monthly_cap_usd = float(raw["saas_monthly_cap_usd"])
        self.hardware_capital_over_usd = float(raw["hardware_capital_over_usd"])
        self.travel_manager_approval_over_usd = float(raw["travel_manager_approval_over_usd"])
        self.fx_hard_stop_usd = float(raw["fx_hard_stop_usd"])
        self.fx_rates = {code.upper(): float(rate) for code, rate in raw.get("fx_rates", {}).items()}
        self.known_vendors = {normalize_vendor(vendor) for vendor in raw.get("known_vendors", [])}


def _resolve_path(path: str | None) -> str:
    if path:
        return path
    env_path = os.getenv("POLICY_CONFIG_PATH")
    if env_path:
        return env_path
    # repo-relative fallback for bare local runs and tests; inside a container
    # the source sits too close to the filesystem root for this to exist
    parents = Path(__file__).resolve().parents
    if len(parents) >= 4:
        return str(parents[3] / "data" / "policy-config.json")
    raise FileNotFoundError("POLICY_CONFIG_PATH is not set and no repo-relative config exists")


_cache: tuple[str, float, PolicyConfig] | None = None


def load_policy(path: str | None = None) -> PolicyConfig:
    """Load the policy config, re-reading the file only when its mtime changes.

    Edits to the mounted config apply on the next evaluation without a restart.
    """
    global _cache
    resolved = _resolve_path(path)
    mtime = os.path.getmtime(resolved)
    if _cache and _cache[0] == resolved and _cache[1] == mtime:
        return _cache[2]
    with open(resolved, encoding="utf-8") as f:
        config = PolicyConfig(json.load(f))
    _cache = (resolved, mtime, config)
    return config
