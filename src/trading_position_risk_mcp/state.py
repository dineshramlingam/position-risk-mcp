"""Persistent risk state: equity, peak, today's P&L, open positions, pending approvals.

`JsonFileStore` is the local default (file lock + atomic write). To run on AWS, implement
the same `transaction()` / `read()` interface on DynamoDB with a conditional write on a
version attribute.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Literal, Protocol

from pydantic import BaseModel, Field

JOURNAL_LIMIT = 500


class Position(BaseModel):
    id: str
    symbol: str
    kind: Literal["future", "option"]
    side: Literal["long", "short"]
    direction: int                      # +1 profits when price rises (or when you paid premium)
    contracts: int
    entry_price: Decimal
    stop_price: Decimal | None = None
    risk_per_contract: Decimal
    point_value: Decimal
    commission_rt: Decimal              # per contract (per unit for options)
    slippage_ticks: int = 0
    tick_size: Decimal = Decimal("0.01")
    bucket: str
    margin_per_contract: Decimal | None = None
    structure: dict | None = None
    approval_id: str
    opened_at: datetime

    @property
    def open_risk(self) -> Decimal:
        return self.risk_per_contract * self.contracts


class Approval(BaseModel):
    id: str
    symbol: str
    kind: Literal["future", "option"]
    side: Literal["long", "short"]
    direction: int
    contracts: int
    entry_price: Decimal
    stop_price: Decimal | None = None
    risk_per_contract: Decimal
    point_value: Decimal
    commission_rt: Decimal
    slippage_ticks: int = 0
    bucket: str
    margin_per_contract: Decimal | None = None
    structure: dict | None = None
    created_at: datetime
    expires_at: datetime


class DayState(BaseModel):
    date: str
    start_equity: Decimal | None = None   # equity at the day's open; the daily limit is based on this
    realized_pnl: Decimal = Decimal(0)
    trades_closed: int = 0
    locked: bool = False
    lock_reason: str | None = None


class RiskState(BaseModel):
    equity: Decimal
    peak_equity: Decimal
    day: DayState
    positions: dict[str, Position] = Field(default_factory=dict)
    approvals: dict[str, Approval] = Field(default_factory=dict)
    journal: list[dict] = Field(default_factory=list)
    created_with_rulebook: str | None = None

    def log(self, event: str, **data) -> None:
        entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event}
        entry.update(json.loads(json.dumps(data, default=str)))
        self.journal.append(entry)
        if len(self.journal) > JOURNAL_LIMIT:
            del self.journal[: len(self.journal) - JOURNAL_LIMIT]


class StateStore(Protocol):
    def transaction(self) -> "contextmanager[RiskState]": ...
    def read(self) -> RiskState: ...


class JsonFileStore:
    def __init__(self, path: str | Path, factory: Callable[[], RiskState]):
        self.path = Path(path)
        self.factory = factory
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _load(self) -> RiskState:
        if not self.path.exists():
            return self.factory()
        return RiskState.model_validate_json(self.path.read_text())

    def _write(self, state: RiskState) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(state.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    @contextmanager
    def transaction(self) -> Iterator[RiskState]:
        with open(self._lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = self._load()
                yield state
                self._write(state)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self) -> RiskState:
        with open(self._lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                return self._load()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
