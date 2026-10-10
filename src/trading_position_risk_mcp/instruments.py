"""Instrument registry: contract specs loaded from instruments.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml

from .util import to_dec


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    kind: Literal["future", "option"]
    point_value: Decimal          # $ per 1.0 point per contract (multiplier for options)
    tick_size: Decimal
    bucket: str
    micro: str | None = None
    is_micro: bool = False
    margin_per_contract: Decimal | None = None

    @property
    def tick_value(self) -> Decimal:
        return self.point_value * self.tick_size

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "kind": self.kind,
            "point_value": float(self.point_value),
            "tick_size": float(self.tick_size),
            "tick_value": float(self.tick_value),
            "bucket": self.bucket,
            "micro": self.micro,
            "is_micro": self.is_micro,
            "margin_per_contract": None if self.margin_per_contract is None else float(self.margin_per_contract),
        }


class UnknownInstrumentError(KeyError):
    pass


def normalize_symbol(symbol: str) -> str:
    """'/es', 'ES', ' es ' -> 'ES'. Drops a trailing contract month like 'ESZ6' -> 'ES' when known."""
    return symbol.strip().lstrip("/").upper()


class InstrumentRegistry:
    def __init__(self, instruments: dict[str, Instrument]):
        self._by_symbol = instruments

    @classmethod
    def from_yaml(cls, path: str | Path) -> "InstrumentRegistry":
        raw = yaml.safe_load(Path(path).read_text())
        out: dict[str, Instrument] = {}
        for kind, section in (("future", "futures"), ("option", "options")):
            for sym, spec in (raw.get(section) or {}).items():
                sym = normalize_symbol(sym)
                margin = spec.get("margin_per_contract")
                out[sym] = Instrument(
                    symbol=sym,
                    name=spec.get("name", sym),
                    kind=kind,  # type: ignore[arg-type]
                    point_value=to_dec(spec["point_value"]),
                    tick_size=to_dec(spec["tick_size"]),
                    bucket=spec.get("bucket", sym),
                    micro=normalize_symbol(spec["micro"]) if spec.get("micro") else None,
                    is_micro=bool(spec.get("is_micro", False)),
                    margin_per_contract=None if margin is None else to_dec(margin),
                )
        for inst in out.values():
            if inst.micro and inst.micro not in out:
                raise ValueError(f"{inst.symbol}: micro '{inst.micro}' is not defined")
        return cls(out)

    def get(self, symbol: str) -> Instrument:
        sym = normalize_symbol(symbol)
        if sym in self._by_symbol:
            return self._by_symbol[sym]
        # Accept dated futures codes such as ESZ6 / ESZ26 / MESH7.
        for root in sorted(self._by_symbol, key=len, reverse=True):
            rest = sym[len(root):]
            if sym.startswith(root) and 2 <= len(rest) <= 3 and rest[0] in "FGHJKMNQUVXZ" and rest[1:].isdigit():
                return self._by_symbol[root]
        raise UnknownInstrumentError(f"Unknown instrument '{symbol}'. Known: {', '.join(sorted(self._by_symbol))}")

    def all(self) -> list[Instrument]:
        return [self._by_symbol[s] for s in sorted(self._by_symbol)]
