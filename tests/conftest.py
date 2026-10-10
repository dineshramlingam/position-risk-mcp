from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trading_position_risk_mcp.engine import RiskEngine
from trading_position_risk_mcp.instruments import InstrumentRegistry
from trading_position_risk_mcp.rules import load_rulebook
from trading_position_risk_mcp.state import JsonFileStore

CONFIG = Path(__file__).resolve().parents[1] / "config"
NY = ZoneInfo("America/New_York")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 9, 10, 0, tzinfo=NY)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def registry():
    return InstrumentRegistry.from_yaml(CONFIG / "instruments.yaml")


@pytest.fixture
def rules():
    return load_rulebook(CONFIG / "rules.yaml")


@pytest.fixture
def engine(rules, registry, tmp_path, clock):
    e = RiskEngine(rules, registry, store=None, now=clock)  # type: ignore[arg-type]
    e.store = JsonFileStore(tmp_path / "state.json", factory=e.new_state)
    return e
