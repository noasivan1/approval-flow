import json
import os
from pathlib import Path

from app.policy import load_policy

_REPO_CONFIG = Path(__file__).resolve().parents[3] / "data" / "policy-config.json"


def write_config(path: Path, ceiling: float) -> None:
    raw = json.loads(_REPO_CONFIG.read_text(encoding="utf-8"))
    raw["autonomy_ceiling_usd"] = ceiling
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_env_path_is_used_directly(tmp_path, monkeypatch):
    config_file = tmp_path / "policy.json"
    write_config(config_file, 999)
    monkeypatch.setenv("POLICY_CONFIG_PATH", str(config_file))

    assert load_policy().autonomy_ceiling_usd == 999


def test_explicit_path_wins_over_env(tmp_path, monkeypatch):
    env_file = tmp_path / "env.json"
    explicit_file = tmp_path / "explicit.json"
    write_config(env_file, 111)
    write_config(explicit_file, 222)
    monkeypatch.setenv("POLICY_CONFIG_PATH", str(env_file))

    assert load_policy(str(explicit_file)).autonomy_ceiling_usd == 222


def test_repo_fallback_when_env_unset(monkeypatch):
    monkeypatch.delenv("POLICY_CONFIG_PATH", raising=False)
    config = load_policy()
    assert config.autonomy_ceiling_usd == 250


def test_config_reloads_when_file_changes(tmp_path, monkeypatch):
    config_file = tmp_path / "policy.json"
    write_config(config_file, 100)
    monkeypatch.setenv("POLICY_CONFIG_PATH", str(config_file))
    assert load_policy().autonomy_ceiling_usd == 100

    write_config(config_file, 300)
    # bump mtime explicitly; fast consecutive writes can share a timestamp
    stat = os.stat(config_file)
    os.utime(config_file, (stat.st_atime, stat.st_mtime + 1))

    assert load_policy().autonomy_ceiling_usd == 300
