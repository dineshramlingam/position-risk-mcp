"""Build the engine from environment-configured paths."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .engine import RiskEngine
from .instruments import InstrumentRegistry
from .rules import load_rulebook
from .state import JsonFileStore

_PACKAGED_DEFAULTS = Path(__file__).resolve().parent / "defaults"      # present in wheels and sdists
_REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"          # present in a source checkout


def _fallback(filename: str) -> Path:
    for d in (_PACKAGED_DEFAULTS, _REPO_CONFIG):
        if (d / filename).exists():
            return d / filename
    return _REPO_CONFIG / filename


def _path(env: str, filename: str) -> Path:
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser()
    local = Path.cwd() / "config" / filename
    return local if local.exists() else _fallback(filename)


def build_engine() -> RiskEngine:
    rules = load_rulebook(_path("POSITION_RISK_RULES", "rules.yaml"))
    registry = InstrumentRegistry.from_yaml(_path("POSITION_RISK_INSTRUMENTS", "instruments.yaml"))
    state_path = Path(os.environ.get("POSITION_RISK_STATE", "~/.position-risk-mcp/risk_state.json")).expanduser()
    engine = RiskEngine(rules, registry, store=None)  # type: ignore[arg-type]
    engine.store = JsonFileStore(state_path, factory=engine.new_state)
    return engine


@lru_cache(maxsize=1)
def get_engine() -> RiskEngine:
    return build_engine()
