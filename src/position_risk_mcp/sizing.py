"""Pure position-sizing math. No state, no I/O.

Core formula (same as the E-mini spreadsheet):

    contracts = floor( equity * risk% / risk_per_contract )
    risk_per_contract = stop_points * point_value  (+ commission + slippage when costs are on)

Option structures are sized from their defined maximum loss.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from .instruments import Instrument
from .util import floor_int, money, pct, round_to_tick_away, to_dec

Side = Literal["long", "short"]


class SizingError(ValueError):
    """Input that cannot be sized (bad stop, missing fields, ...)."""


class UndefinedRiskError(SizingError):
    """The structure has no defined maximum loss, so it cannot be sized and is always rejected."""


# --------------------------------------------------------------------------- futures

@dataclass
class StopSpec:
    stop_price: Decimal
    stop_points: Decimal
    adjusted: bool = False
    source: str = "stop"   # "stop" | "atr"


def resolve_stop(
    inst: Instrument,
    side: Side,
    entry,
    stop=None,
    atr=None,
    atr_multiple=2.0,
) -> StopSpec:
    """Validate the stop (or derive it from ATR) and snap it to the tick grid, away from entry."""
    if side not in ("long", "short"):
        raise SizingError("side must be 'long' or 'short'")
    entry = to_dec(entry)
    if entry <= 0:
        raise SizingError("entry must be > 0")
    source = "stop"
    if stop is None:
        if atr is None:
            raise SizingError("a stop price (or atr + atr_multiple) is required for futures")
        dist = to_dec(atr) * to_dec(atr_multiple)
        if dist <= 0:
            raise SizingError("atr * atr_multiple must be > 0")
        stop = entry - dist if side == "long" else entry + dist
        source = "atr"
    stop = to_dec(stop)
    if side == "long" and stop >= entry:
        raise SizingError(f"long stop {stop} must be below entry {entry}")
    if side == "short" and stop <= entry:
        raise SizingError(f"short stop {stop} must be above entry {entry}")
    snapped = round_to_tick_away(stop, entry, inst.tick_size)
    return StopSpec(
        stop_price=snapped,
        stop_points=abs(entry - snapped),
        adjusted=(snapped != stop),
        source=source,
    )


def futures_risk_per_contract(
    inst: Instrument, stop_points: Decimal, commission_rt: Decimal = Decimal(0), slippage_ticks: int = 0
) -> dict[str, Decimal]:
    price_risk = stop_points * inst.point_value
    slippage = inst.tick_value * slippage_ticks
    return {
        "price_risk": price_risk,
        "slippage": slippage,
        "commission": commission_rt,
        "total": price_risk + slippage + commission_rt,
    }


# --------------------------------------------------------------------------- options

# direction: +1 = you pay (long premium / debit), -1 = you collect (credit)
_DEFINED = {
    "long_call":       {"legs": 1, "direction": +1, "needs": ("premium",)},
    "long_put":        {"legs": 1, "direction": +1, "needs": ("premium",)},
    "debit_spread":    {"legs": 2, "direction": +1, "needs": ("debit",)},
    "butterfly":       {"legs": 3, "direction": +1, "needs": ("debit",), "contracts_per_unit": 4},
    "credit_spread":   {"legs": 2, "direction": -1, "needs": ("width", "credit")},
    "iron_condor":     {"legs": 4, "direction": -1, "needs": ("width", "credit")},
    "iron_butterfly":  {"legs": 4, "direction": -1, "needs": ("width", "credit")},
}
_UNDEFINED = {
    "short_call", "short_put", "naked_call", "naked_put", "short_straddle",
    "short_strangle", "ratio_spread", "front_ratio", "short_stock",
}
OPTION_STRUCTURES = sorted(_DEFINED)


@dataclass
class OptionRisk:
    structure: str
    direction: int
    entry_price: Decimal        # premium / debit / credit per unit, in points
    max_loss_points: Decimal
    contracts_per_unit: int
    breakdown: dict[str, Decimal]

    @property
    def total(self) -> Decimal:
        return self.breakdown["total"]


def option_risk_per_unit(inst: Instrument, structure: dict, commission_per_contract_rt: Decimal) -> OptionRisk:
    """Max loss for one unit of a defined-risk structure (one spread, one condor, ...)."""
    kind = str(structure.get("type", "")).lower()
    if kind in _UNDEFINED:
        raise UndefinedRiskError(f"'{kind}' has undefined risk and cannot be sized; use a defined-risk structure")
    if kind not in _DEFINED:
        raise SizingError(f"unknown option structure '{kind}'. Supported: {', '.join(OPTION_STRUCTURES)}")
    spec = _DEFINED[kind]
    vals = {}
    for k in spec["needs"]:
        if structure.get(k) is None:
            raise SizingError(f"{kind} requires '{k}'")
        vals[k] = to_dec(structure[k])
        if vals[k] <= 0:
            raise SizingError(f"{k} must be > 0")
    if spec["direction"] > 0:
        entry = vals.get("premium", vals.get("debit"))
        max_loss = entry
    else:
        if vals["credit"] >= vals["width"]:
            raise SizingError("credit must be less than the spread width")
        entry = vals["credit"]
        max_loss = vals["width"] - vals["credit"]
    contracts_per_unit = spec.get("contracts_per_unit", spec["legs"])
    price_risk = max_loss * inst.point_value
    commission = commission_per_contract_rt * contracts_per_unit
    return OptionRisk(
        structure=kind,
        direction=spec["direction"],
        entry_price=entry,
        max_loss_points=max_loss,
        contracts_per_unit=contracts_per_unit,
        breakdown={"price_risk": price_risk, "slippage": Decimal(0), "commission": commission,
                   "total": price_risk + commission},
    )


# --------------------------------------------------------------------------- sizing

@dataclass
class SizeResult:
    symbol: str
    contracts: int
    risk_budget: Decimal
    risk_per_contract: Decimal
    breakdown: dict[str, Decimal]
    equity: Decimal
    risk_pct_requested: Decimal
    risk_pct_effective: Decimal
    method: str
    stop_price: Decimal | None = None
    stop_points: Decimal | None = None
    warnings: list[str] = field(default_factory=list)
    micro_alternative: "SizeResult | None" = None

    @property
    def total_risk(self) -> Decimal:
        return self.risk_per_contract * self.contracts

    def to_dict(self) -> dict:
        d = {
            "symbol": self.symbol,
            "contracts": self.contracts,
            "method": self.method,
            "equity": money(self.equity),
            "risk_pct_requested": float(self.risk_pct_requested),
            "risk_pct_effective": float(self.risk_pct_effective),
            "risk_budget": money(self.risk_budget),
            "risk_per_contract": money(self.risk_per_contract),
            "risk_per_contract_breakdown": {k: money(v) for k, v in self.breakdown.items()},
            "total_risk": money(self.total_risk),
            "total_risk_pct_of_equity": pct(self.total_risk / self.equity) if self.equity else None,
            "unused_budget": money(self.risk_budget - self.total_risk),
            "warnings": self.warnings,
        }
        if self.stop_price is not None:
            d["stop_price"] = float(self.stop_price)
            d["stop_points"] = float(self.stop_points)
        if self.micro_alternative is not None:
            d["micro_alternative"] = self.micro_alternative.to_dict()
        return d


def contracts_for_budget(budget: Decimal, risk_per_contract: Decimal) -> int:
    if risk_per_contract <= 0:
        raise SizingError("risk per contract must be > 0")
    if budget <= 0:
        return 0
    return floor_int(budget / risk_per_contract)


def size_fixed_fractional(
    *,
    equity,
    inst: Instrument,
    risk_per_contract: Decimal,
    breakdown: dict[str, Decimal],
    risk_pct,
    size_multiplier=Decimal(1),
    max_contracts: int | None = None,
    method: str = "fixed_fractional",
    stop: StopSpec | None = None,
) -> SizeResult:
    equity, risk_pct, size_multiplier = to_dec(equity), to_dec(risk_pct), to_dec(size_multiplier)
    if equity <= 0:
        raise SizingError("equity must be > 0")
    effective_pct = risk_pct * size_multiplier
    budget = equity * effective_pct / 100
    n = contracts_for_budget(budget, risk_per_contract)
    warnings: list[str] = []
    if max_contracts is not None and n > max_contracts:
        warnings.append(f"capped at max_contracts={max_contracts} (budget allowed {n})")
        n = max_contracts
    if stop is not None and stop.adjusted:
        warnings.append(f"stop snapped to tick grid at {stop.stop_price} (moved away from entry)")
    if n == 0:
        warnings.append(
            f"risk per contract ${money(risk_per_contract)} exceeds budget ${money(budget)}: size is 0"
        )
    return SizeResult(
        symbol=inst.symbol,
        contracts=n,
        risk_budget=budget,
        risk_per_contract=risk_per_contract,
        breakdown=breakdown,
        equity=equity,
        risk_pct_requested=risk_pct,
        risk_pct_effective=effective_pct,
        method=method,
        stop_price=None if stop is None else stop.stop_price,
        stop_points=None if stop is None else stop.stop_points,
        warnings=warnings,
    )


def spreadsheet_size(equity, stop_points, risk_pct, point_value) -> int:
    """Exact replica of the spreadsheet: no costs, round down."""
    budget = to_dec(equity) * to_dec(risk_pct) / 100
    return contracts_for_budget(budget, to_dec(stop_points) * to_dec(point_value))
