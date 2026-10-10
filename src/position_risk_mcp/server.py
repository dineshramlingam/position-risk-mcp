"""MCP server exposing position sizing and the pre-trade risk gate to trading agents.

Run:  position-risk-mcp                (stdio, for Claude Desktop / agent frameworks)
      position-risk-mcp --http 8765    (streamable HTTP)
"""
from __future__ import annotations

import argparse
import os
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from .auth import TOKEN_ENV, BearerAuth, check_bind
from .config import get_engine
from .engine import RiskEngineError
from .instruments import UnknownInstrumentError
from .sizing import OPTION_STRUCTURES, SizingError

INSTRUCTIONS = """\
Deterministic position sizing and risk gate. Never compute position size yourself.

Required order flow:
  1. check_trade(...)  -> verdict APPROVED / RESIZED / REJECTED, approved_contracts, approval_id
  2. Only if APPROVED or RESIZED: place the order for AT MOST approved_contracts, before the approval expires.
     If you do not place it, call cancel_approval: a pending approval reserves risk until filled, cancelled or expired.
  3. record_fill(approval_id, fill_price) immediately after the fill. Check `breaches` in the response.
  4. update_stop(...) to trail stops (tightening only).
  5. record_close(position_id, exit_price) when flat; this updates equity, daily P&L and lockouts.

Account equity and the rulebook are owned by the human and cannot be changed through this server.
If a verdict is REJECTED, do not trade it. If a `suggestion` is present (e.g. use MES instead of ES),
you may run check_trade again with the suggested symbol.
"""

mcp = FastMCP("position-risk", instructions=INSTRUCTIONS)


class OptionStructure(BaseModel):
    """A defined-risk option structure. Prices are per unit, in points (e.g. 1.20 = $120 for SPX)."""
    type: Annotated[str, Field(description=f"One of: {', '.join(OPTION_STRUCTURES)}. "
                                           "Naked/short premium structures are rejected.")]
    premium: float | None = Field(None, description="long_call / long_put: premium paid")
    debit: float | None = Field(None, description="debit_spread / butterfly: net debit paid")
    credit: float | None = Field(None, description="credit_spread / iron_condor / iron_butterfly: net credit")
    width: float | None = Field(None, description="credit structures: strike width of the widest wing")


def _run(fn, **kw):
    try:
        return fn(**kw)
    except (RiskEngineError, SizingError, UnknownInstrumentError) as e:
        raise ToolError(str(e)) from e


def _struct(s: OptionStructure | None) -> dict | None:
    return None if s is None else s.model_dump(exclude_none=True)


@mcp.tool()
def get_instrument_spec(symbol: str) -> dict:
    """Contract specs for a symbol (ES, MES, NQ, /MNQ, ESZ6, SPX...): point value, tick size and value,
    exposure bucket, micro equivalent and margin (if configured)."""
    return _run(lambda symbol: get_engine().registry.get(symbol).to_dict(), symbol=symbol)


@mcp.tool()
def list_instruments() -> list[dict]:
    """All supported instruments and their specs."""
    return [i.to_dict() for i in get_engine().registry.all()]


@mcp.tool()
def calculate_position_size(
    symbol: str,
    side: Literal["long", "short"] | None = None,
    entry: float | None = None,
    stop: float | None = None,
    atr: Annotated[float | None, Field(description="Use instead of stop: stop = entry -/+ atr * atr_multiple")] = None,
    atr_multiple: float = 2.0,
    structure: OptionStructure | None = None,
    risk_pct: Annotated[float | None, Field(description="% of equity to risk; defaults to the rulebook "
                                                        "default and is capped at its max")] = None,
    include_costs: Annotated[bool, Field(description="Add commission and stop slippage to risk per contract. "
                                                     "False reproduces the classic spreadsheet.")] = True,
) -> dict:
    """Size ONE trade with fixed-fractional risk: floor(equity x risk% / risk_per_contract).
    Read-only and informational; it ignores other open positions and issues no approval.
    Futures need side + entry + (stop or atr). Options need `structure`.
    If the full-size contract rounds to 0, a micro alternative is returned (ES -> MES)."""
    return _run(get_engine().calculate_position_size, symbol=symbol, side=side, entry=entry, stop=stop,
                atr=atr, atr_multiple=atr_multiple, structure=_struct(structure), risk_pct=risk_pct,
                include_costs=include_costs)


@mcp.tool()
def check_trade(
    symbol: str,
    side: Literal["long", "short"] | None = None,
    entry: float | None = None,
    stop: float | None = None,
    atr: float | None = None,
    atr_multiple: float = 2.0,
    structure: OptionStructure | None = None,
    contracts: Annotated[int | None, Field(description="Requested size. Omit to get the maximum allowed.")] = None,
    risk_pct: float | None = None,
) -> dict:
    """THE PRE-TRADE GATE. Call before every order. Applies per-trade risk, portfolio heat, exposure bucket,
    daily-loss budget, drawdown tiers, open-position count, margin and contract caps.
    Returns verdict APPROVED / RESIZED / REJECTED, approved_contracts, the rules that fired, and an
    approval_id (needed for record_fill) that expires after a few minutes."""
    return _run(get_engine().check_trade, symbol=symbol, side=side, entry=entry, stop=stop, atr=atr,
                atr_multiple=atr_multiple, structure=_struct(structure), contracts=contracts, risk_pct=risk_pct)


@mcp.tool()
def record_fill(
    approval_id: str,
    fill_price: Annotated[float, Field(description="Futures: fill price. Options: actual premium/debit/credit per unit.")],
    contracts: Annotated[int | None, Field(description="Filled quantity; defaults to the approved size")] = None,
    stop: Annotated[float | None, Field(description="Futures: actual stop placed; defaults to the approved stop")] = None,
) -> dict:
    """Record a broker fill against an approval. Opens a tracked position and returns its position_id.
    Fails if the approval is unknown/expired or the quantity exceeds what was approved."""
    return _run(get_engine().record_fill, approval_id=approval_id, fill_price=fill_price,
                contracts=contracts, stop=stop)


@mcp.tool()
def cancel_approval(approval_id: str) -> dict:
    """Release the risk reserved by an approval you will not use (order not placed or rejected by the broker).
    Pending approvals count against portfolio, bucket, daily and position limits until filled, cancelled
    or expired."""
    return _run(get_engine().cancel_approval, approval_id=approval_id)


@mcp.tool()
def update_stop(position_id: str, new_stop: float) -> dict:
    """Move a futures position's stop. Tightening (or trailing into profit) is allowed; widening is
    rejected unless the rulebook permits it. Updates open risk."""
    return _run(get_engine().update_stop, position_id=position_id, new_stop=new_stop)


@mcp.tool()
def record_close(
    position_id: str,
    exit_price: Annotated[float, Field(description="Futures: exit price. Options: price per unit paid/received "
                                                   "to close (0 if expired worthless).")],
    contracts: Annotated[int | None, Field(description="Partial close quantity; defaults to all")] = None,
) -> dict:
    """Close (all or part of) a position. Updates equity, peak, daily P&L and triggers the daily-loss
    lockout or drawdown tiers when limits are hit."""
    return _run(get_engine().record_close, position_id=position_id, exit_price=exit_price, contracts=contracts)


@mcp.tool()
def get_risk_state() -> dict:
    """Current equity, drawdown and size multiplier, daily P&L and remaining loss budget, lockout status,
    open positions with risk, bucket exposure, pending approvals and recent journal entries."""
    return get_engine().risk_state()


@mcp.tool()
def get_rulebook() -> dict:
    """The active risk rulebook (read-only), with its version and fingerprint."""
    rb = get_engine().rules
    return rb.model_dump(mode="json")


def main() -> None:
    ap = argparse.ArgumentParser(description="Position sizing & risk MCP server")
    ap.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP on PORT instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    get_engine()  # fail fast on bad config
    if args.http:
        import uvicorn

        token = os.environ.get(TOKEN_ENV) or None
        check_bind(args.host, token)
        mcp.settings.host, mcp.settings.port = args.host, args.http
        uvicorn.run(BearerAuth(mcp.streamable_http_app(), token), host=args.host, port=args.http)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
