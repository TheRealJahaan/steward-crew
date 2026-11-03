"""Where Steward keeps its files, and the policy it runs under."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY: dict[str, Any] = {
    "repo": "",
    "maintainers": [],
    "rooms": {"community": "help", "maintainers": "maintainers"},
    "agents": {"helper": "stu", "steward": "steward", "reviewer": "reviewer"},
    "claims": {"quiet_after_hours": 48, "max_open_per_person": 1, "newcomer_max_open": 1},
    "pull_requests": {"large_change_lines": 400, "new_account_days": 14},
    "digest_at": "09:00",
    "sync_every_seconds": 60,
    "demo_mode": True,
}


def home() -> Path:
    """Steward's folder. STEWARD_HOME overrides it (the tests use this)."""
    path = Path(os.environ.get("STEWARD_HOME", Path.home() / ".steward"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return home() / "steward.db"


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Policy:
    raw: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_POLICY))

    @property
    def repo(self) -> str:
        return str(self.raw.get("repo") or "")

    @property
    def maintainers(self) -> list[str]:
        return [str(m).strip().lower() for m in self.raw.get("maintainers") or []]

    @property
    def quiet_hours(self) -> float:
        return float(self.raw["claims"]["quiet_after_hours"])

    @property
    def max_open_claims(self) -> int:
        return int(self.raw["claims"]["max_open_per_person"])

    @property
    def large_change_lines(self) -> int:
        return int(self.raw["pull_requests"]["large_change_lines"])

    @property
    def new_account_days(self) -> int:
        return int(self.raw["pull_requests"]["new_account_days"])

    def room(self, key: str) -> str:
        return str(self.raw["rooms"][key])

    def agent(self, key: str) -> str:
        return str(self.raw["agents"][key])

    @property
    def demo_mode(self) -> bool:
        return bool(self.raw.get("demo_mode", True))

    def is_maintainer(self, name: str) -> bool:
        if not self.maintainers:
            return True
        return (name or "").strip().lower() in self.maintainers


def load_policy(path: Path | None = None) -> Policy:
    path = path or home() / "policy.yaml"
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Policy(_merge(DEFAULT_POLICY, data))
