"""Small numeric helpers. All money math uses Decimal so floor() never lands on 10.999…"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal


def to_dec(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        raise ValueError("expected a number, got None")
    return Decimal(str(x))


def floor_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


def money(x: Decimal | None) -> float | None:
    if x is None:
        return None
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def pct(x: Decimal | None) -> float | None:
    """Decimal fraction -> percent with 3 decimals (0.01 -> 1.0)."""
    if x is None:
        return None
    return float((x * 100).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def round_to_tick_away(price: Decimal, entry: Decimal, tick: Decimal) -> Decimal:
    """Snap a stop price onto the tick grid, moving it AWAY from entry (the conservative direction)."""
    steps = price / tick
    if price < entry:
        snapped = steps.to_integral_value(rounding=ROUND_FLOOR) * tick
    else:
        snapped = steps.to_integral_value(rounding=ROUND_CEILING) * tick
    return snapped
