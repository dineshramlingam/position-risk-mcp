"""Risk engine: sizing + the pre-trade gate + position/P&L bookkeeping.

Lifecycle an agent must follow:
    check_trade  ->  (place order with broker)  ->  record_fill  ->  update_stop*  ->  record_close
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Callable
from zoneinfo import ZoneInfo

from .instruments import Instrument, InstrumentRegistry, UnknownInstrumentError
from .rules import Rulebook
from .sizing import (
    SizingError,
    StopSpec,
    UndefinedRiskError,
    contracts_for_budget,
    futures_risk_per_contract,
    option_risk_per_unit,
    resolve_stop,
    size_fixed_fractional,
)
from .state import Approval, DayState, Position, RiskState, StateStore
from .util import floor_int, money, pct, round_to_tick_away, to_dec

APPROVED, RESIZED, REJECTED = "APPROVED", "RESIZED", "REJECTED"


class RiskEngineError(ValueError):
    pass


class _Priced:
    """A trade idea priced into risk-per-contract."""

    def __init__(self, *, inst, kind, side, direction, entry, stop: StopSpec | None, rpc, breakdown,
                 commission_rt, slippage_ticks, structure=None):
        self.inst: Instrument = inst
        self.kind = kind
        self.side = side
        self.direction = direction
        self.entry: Decimal = entry
        self.stop = stop
        self.rpc: Decimal = rpc
        self.breakdown = breakdown
        self.commission_rt = commission_rt
        self.slippage_ticks = slippage_ticks
        self.structure = structure


class RiskEngine:
    def __init__(self, rules: Rulebook, registry: InstrumentRegistry, store: StateStore,
                 now: Callable[[], datetime] | None = None):
        self.rules = rules
        self.registry = registry
        self.store = store
        self.tz = ZoneInfo(rules.account.timezone)
        self._now = now or (lambda: datetime.now(self.tz))

    # ------------------------------------------------------------------ state helpers

    def new_state(self) -> RiskState:
        eq = self.rules.account.starting_equity
        return RiskState(equity=eq, peak_equity=eq, day=DayState(date=self._today(), start_equity=eq),
                         created_with_rulebook=self.rules.fingerprint)

    def _today(self) -> str:
        return self._now().astimezone(self.tz).date().isoformat()

    def _housekeeping(self, s: RiskState) -> None:
        today = self._today()
        if s.day.date != today:
            s.log("day_rollover", previous=s.day.model_dump(mode="json"))
            s.day = DayState(date=today, start_equity=s.equity)
        now = self._now()
        for aid in [a.id for a in s.approvals.values() if a.expires_at <= now]:
            del s.approvals[aid]

    def _drawdown_pct(self, s: RiskState) -> Decimal:
        if s.peak_equity <= 0 or s.equity >= s.peak_equity:
            return Decimal(0)
        return (s.peak_equity - s.equity) / s.peak_equity * 100

    def _open_risk(self, s: RiskState, bucket: str | None = None) -> Decimal:
        return sum((p.open_risk for p in s.positions.values() if bucket is None or p.bucket == bucket), Decimal(0))

    def _margin_used(self, s: RiskState) -> Decimal:
        return sum((p.margin_per_contract * p.contracts for p in s.positions.values() if p.margin_per_contract),
                   Decimal(0))

    # Approvals that have been issued but not yet filled reserve risk, so an agent cannot collect several
    # full-size approvals and fill them all. A new check_trade for a symbol supersedes that symbol's
    # earlier pending approval, so re-checking the same idea doesn't double-reserve.

    def _pending(self, s: RiskState, exclude_symbol: str | None = None):
        return [a for a in s.approvals.values() if a.symbol != exclude_symbol]

    def _committed_risk(self, s: RiskState, bucket: str | None = None, exclude_symbol: str | None = None) -> Decimal:
        pend = sum((a.risk_per_contract * a.contracts for a in self._pending(s, exclude_symbol)
                    if bucket is None or a.bucket == bucket), Decimal(0))
        return self._open_risk(s, bucket) + pend

    def _committed_margin(self, s: RiskState, exclude_symbol: str | None = None) -> Decimal:
        pend = sum((a.margin_per_contract * a.contracts for a in self._pending(s, exclude_symbol)
                    if a.margin_per_contract), Decimal(0))
        return self._margin_used(s) + pend

    def _daily_limit(self, s: RiskState) -> Decimal:
        base = s.day.start_equity if s.day.start_equity is not None else s.equity
        return base * self.rules.daily.max_daily_loss_pct / 100

    def _context(self, s: RiskState) -> dict:
        dd = self._drawdown_pct(s)
        mult, tier = self.rules.drawdown_multiplier(dd)
        open_risk = self._open_risk(s)
        limit = self._daily_limit(s)
        buckets: dict[str, Decimal] = {}
        for p in s.positions.values():
            buckets[p.bucket] = buckets.get(p.bucket, Decimal(0)) + p.open_risk
        return {
            "equity": money(s.equity),
            "peak_equity": money(s.peak_equity),
            "drawdown_pct": float(dd.quantize(Decimal("0.01"))),
            "size_multiplier": float(mult),
            "active_drawdown_tier": None if tier is None else tier.model_dump(mode="json"),
            "trading_halted": mult == 0,
            "day": s.day.date,
            "daily_realized_pnl": money(s.day.realized_pnl),
            "daily_loss_limit": money(limit),
            "daily_loss_remaining": money(max(Decimal(0), limit + min(s.day.realized_pnl, Decimal(0)))),
            "daily_locked": s.day.locked,
            "daily_lock_reason": s.day.lock_reason,
            "open_positions": len(s.positions),
            "open_risk": money(open_risk),
            "pending_approval_risk": money(self._committed_risk(s) - open_risk),
            "rulebook_changed": s.created_with_rulebook not in (None, self.rules.fingerprint),
            "open_risk_pct": pct(open_risk / s.equity) if s.equity > 0 else None,
            "open_risk_cap_pct": float(self.rules.portfolio.max_open_risk_pct),
            "bucket_risk": {b: {"risk": money(r), "pct": pct(r / s.equity)} for b, r in buckets.items()},
            "margin_used": money(self._margin_used(s)),
        }

    # ------------------------------------------------------------------ pricing

    def _price(self, inst: Instrument, *, side, entry, stop, atr, atr_multiple, structure,
               include_costs=True) -> _Priced:
        if inst.kind == "future":
            if structure:
                raise SizingError(f"{inst.symbol} is a future; 'structure' is only for options")
            if entry is None:
                raise SizingError("entry is required for futures")
            st = resolve_stop(inst, side, entry, stop, atr, atr_multiple)
            comm = self.rules.costs.commission_for(inst.symbol, "future") if include_costs else Decimal(0)
            slip = self.rules.costs.slippage_ticks if include_costs else 0
            bd = futures_risk_per_contract(inst, st.stop_points, comm, slip)
            return _Priced(inst=inst, kind="future", side=side, direction=1 if side == "long" else -1,
                           entry=to_dec(entry), stop=st, rpc=bd["total"], breakdown=bd,
                           commission_rt=comm, slippage_ticks=slip)
        # option
        if not structure:
            raise SizingError(f"{inst.symbol} is an option underlying; pass 'structure' "
                              "(e.g. {'type': 'credit_spread', 'width': 5, 'credit': 1.2})")
        comm_leg = self.rules.costs.commission_for(inst.symbol, "option") if include_costs else Decimal(0)
        orisk = option_risk_per_unit(inst, structure, comm_leg)
        return _Priced(inst=inst, kind="option", side="long" if orisk.direction > 0 else "short",
                       direction=orisk.direction, entry=orisk.entry_price, stop=None, rpc=orisk.total,
                       breakdown=orisk.breakdown, commission_rt=orisk.breakdown["commission"],
                       slippage_ticks=0, structure={**structure, "type": orisk.structure,
                                                    "max_loss_points": str(orisk.max_loss_points)})

    def _clamp_risk_pct(self, risk_pct) -> tuple[Decimal, bool]:
        pt = self.rules.per_trade
        if risk_pct is None:
            return pt.default_risk_pct, False
        r = to_dec(risk_pct)
        if r <= 0:
            raise SizingError("risk_pct must be > 0")
        if r > pt.max_risk_pct:
            return pt.max_risk_pct, True
        return r, False

    def _limits(self, s: RiskState, p: _Priced, risk_pct: Decimal, mult: Decimal) -> dict[str, dict]:
        eq, pr, inst = s.equity, self.rules.portfolio, p.inst
        sym = inst.symbol
        out: dict[str, dict] = {}

        budget = eq * risk_pct * mult / 100
        out["per_trade_risk"] = {"max": contracts_for_budget(budget, p.rpc), "budget": money(budget)}

        heat_rem = eq * pr.max_open_risk_pct / 100 - self._committed_risk(s, None, sym)
        out["portfolio_heat"] = {"max": contracts_for_budget(heat_rem, p.rpc), "budget": money(max(heat_rem, Decimal(0)))}

        b_rem = eq * pr.max_bucket_risk_pct / 100 - self._committed_risk(s, inst.bucket, sym)
        out["bucket_heat"] = {"max": contracts_for_budget(b_rem, p.rpc), "budget": money(max(b_rem, Decimal(0))),
                              "bucket": inst.bucket}

        if self.rules.daily.reserve_open_risk:
            d_rem = self._daily_limit(s) + min(s.day.realized_pnl, Decimal(0)) - self._committed_risk(s, None, sym)
            out["daily_loss_budget"] = {"max": contracts_for_budget(d_rem, p.rpc),
                                        "budget": money(max(d_rem, Decimal(0)))}

        if inst.margin_per_contract and pr.max_margin_utilization_pct:
            m_rem = eq * pr.max_margin_utilization_pct / 100 - self._committed_margin(s, sym)
            out["margin"] = {"max": max(0, floor_int(m_rem / inst.margin_per_contract)), "budget": money(max(m_rem, Decimal(0)))}

        out["max_contracts"] = {"max": self.rules.max_contracts_for(inst.symbol)}
        return out

    # ------------------------------------------------------------------ public: sizing (read-only)

    def calculate_position_size(self, *, symbol, side=None, entry=None, stop=None, atr=None, atr_multiple=2.0,
                                structure=None, risk_pct=None, include_costs=True,
                                apply_drawdown=True) -> dict:
        """What size does the rulebook allow for this one trade, ignoring other open positions?"""
        s = self.store.read()
        self._housekeeping(s)
        inst = self.registry.get(symbol)
        r, clamped = self._clamp_risk_pct(risk_pct)
        mult, tier = self.rules.drawdown_multiplier(self._drawdown_pct(s)) if apply_drawdown else (Decimal(1), None)

        def _size(i: Instrument):
            p = self._price(i, side=side, entry=entry, stop=stop, atr=atr, atr_multiple=atr_multiple,
                            structure=structure, include_costs=include_costs)
            res = size_fixed_fractional(equity=s.equity, inst=i, risk_per_contract=p.rpc, breakdown=p.breakdown,
                                        risk_pct=r, size_multiplier=mult,
                                        max_contracts=self.rules.max_contracts_for(i.symbol),
                                        method="atr" if (p.stop and p.stop.source == "atr") else "fixed_fractional",
                                        stop=p.stop)
            return res

        res = _size(inst)
        if clamped:
            res.warnings.insert(0, f"risk_pct {risk_pct} above max {self.rules.per_trade.max_risk_pct}; capped")
        if tier is not None:
            res.warnings.append(f"drawdown tier active ({tier.from_peak_pct}% from peak): size x{tier.size_multiplier}")
        if res.contracts == 0 and inst.micro:
            res.micro_alternative = _size(self.registry.get(inst.micro))
        out = res.to_dict()
        out["note"] = ("Sizing only. Call check_trade before ordering; it also applies portfolio heat, "
                       "bucket, daily-loss and position-count limits and issues the approval_id.")
        out["account"] = self._context(s)
        out["rulebook"] = self.rules.summary()
        return out

    # ------------------------------------------------------------------ public: the gate

    def check_trade(self, *, symbol, side=None, entry=None, stop=None, atr=None, atr_multiple=2.0,
                    structure=None, contracts=None, risk_pct=None) -> dict:
        with self.store.transaction() as s:
            self._housekeeping(s)
            result = self._check(s, symbol=symbol, side=side, entry=entry, stop=stop, atr=atr,
                                 atr_multiple=atr_multiple, structure=structure, contracts=contracts,
                                 risk_pct=risk_pct)
            s.log("check_trade", symbol=symbol, side=side, requested=contracts, verdict=result["verdict"],
                  approved=result.get("approved_contracts"), approval_id=result.get("approval_id"),
                  rules=[r["rule"] for r in result["reasons"]])
            return result

    def _check(self, s: RiskState, *, symbol, side, entry, stop, atr, atr_multiple, structure, contracts,
               risk_pct) -> dict:
        reasons: list[dict] = []

        def done(verdict, approved=0, **extra):
            out = {"verdict": verdict, "symbol": symbol, "requested_contracts": contracts,
                   "approved_contracts": approved, "reasons": reasons}
            out.update(extra)
            out["account"] = self._context(s)
            out["rulebook"] = self.rules.summary()
            return out

        def reject(rule, msg, **extra):
            reasons.append({"rule": rule, "message": msg})
            return done(REJECTED, **extra)

        try:
            inst = self.registry.get(symbol)
        except UnknownInstrumentError as e:
            return reject("unknown_instrument", str(e))
        symbol = inst.symbol

        if s.day.locked:
            return reject("daily_loss_lockout", f"Trading locked for {s.day.date}: {s.day.lock_reason}. "
                          "Resets next trading day or via the owner's admin CLI.")
        dd = self._drawdown_pct(s)
        mult, tier = self.rules.drawdown_multiplier(dd)
        if mult == 0:
            return reject("drawdown_halt", f"Drawdown {dd:.2f}% from peak reached the halt tier "
                          f"({tier.from_peak_pct}%). Owner must review and reset.")
        n_slots = len(s.positions) + len(self._pending(s, symbol))
        if n_slots >= self.rules.portfolio.max_open_positions:
            return reject("max_open_positions", f"{len(s.positions)} positions open and {n_slots - len(s.positions)} "
                          f"approvals pending; max is {self.rules.portfolio.max_open_positions}")
        if contracts is not None and int(contracts) <= 0:
            return reject("invalid_order", "contracts must be a positive integer")

        try:
            p = self._price(inst, side=side, entry=entry, stop=stop, atr=atr, atr_multiple=atr_multiple,
                            structure=structure)
            r, clamped = self._clamp_risk_pct(risk_pct)
        except UndefinedRiskError as e:
            return reject("undefined_risk", str(e))
        except SizingError as e:
            return reject("invalid_order", str(e))

        if clamped:
            reasons.append({"rule": "per_trade_cap", "message": f"risk_pct {risk_pct} reduced to max "
                            f"{self.rules.per_trade.max_risk_pct}"})
        if tier is not None:
            reasons.append({"rule": "drawdown_reduction", "message": f"Drawdown {dd:.2f}% from peak: size "
                            f"multiplier {tier.size_multiplier}"})
        if p.stop is not None and p.stop.adjusted:
            reasons.append({"rule": "stop_tick_adjusted", "message": f"Stop snapped to {p.stop.stop_price}"})

        limits = self._limits(s, p, r, mult)
        allowed = max(0, min(v["max"] for v in limits.values()))
        binding = [k for k, v in limits.items() if v["max"] == allowed]
        target = allowed if contracts is None else min(int(contracts), allowed)

        risk_info = {
            "risk_pct_used": float(r * mult),
            "risk_per_contract": money(p.rpc),
            "risk_per_contract_breakdown": {k: money(v) for k, v in p.breakdown.items()},
            "total_risk": money(p.rpc * target),
            "total_risk_pct_of_equity": pct(p.rpc * target / s.equity),
        }
        if p.stop is not None:
            risk_info.update(stop_price=float(p.stop.stop_price), stop_points=float(p.stop.stop_points))
        lim_out = {k: v for k, v in limits.items()}

        if target <= 0:
            for b in binding:
                reasons.append({"rule": b, "message": f"{b} allows 0 contracts at ${money(p.rpc)} risk each"})
            extra = {"risk": risk_info, "limits": lim_out, "binding_limits": binding}
            if inst.micro:
                mi = self.registry.get(inst.micro)
                try:
                    mp = self._price(mi, side=side, entry=entry, stop=stop, atr=atr, atr_multiple=atr_multiple,
                                     structure=structure)
                    ml = self._limits(s, mp, r, mult)
                    m_allowed = max(0, min(v["max"] for v in ml.values()))
                    if m_allowed > 0:
                        extra["suggestion"] = {
                            "symbol": mi.symbol, "contracts": m_allowed,
                            "risk_per_contract": money(mp.rpc), "total_risk": money(mp.rpc * m_allowed),
                            "message": f"Resubmit check_trade with symbol={mi.symbol} for up to {m_allowed} contracts",
                        }
                except SizingError:
                    pass
            return done(REJECTED, **extra)

        verdict = APPROVED
        if contracts is not None and int(contracts) > allowed:
            verdict = RESIZED
            reasons.append({"rule": binding[0], "message": f"Requested {contracts}, allowed {allowed} "
                            f"(binding: {', '.join(binding)})"})
        elif clamped and contracts is None:
            verdict = RESIZED

        superseded = [a.id for a in self._pending(s) if a.symbol == symbol]
        for old in superseded:
            del s.approvals[old]
        if superseded:
            reasons.append({"rule": "approval_superseded", "message": f"Replaced earlier pending approval(s) "
                            f"for {symbol}: {', '.join(superseded)}"})
        now = self._now()
        aid = "apr_" + secrets.token_hex(5)
        s.approvals[aid] = Approval(
            id=aid, symbol=symbol, kind=p.kind, side=p.side, direction=p.direction, contracts=target,
            entry_price=p.entry, stop_price=None if p.stop is None else p.stop.stop_price,
            risk_per_contract=p.rpc, point_value=inst.point_value, commission_rt=p.commission_rt,
            slippage_ticks=p.slippage_ticks, bucket=inst.bucket, margin_per_contract=inst.margin_per_contract,
            structure=p.structure, created_at=now,
            expires_at=now + timedelta(seconds=self.rules.approvals.ttl_seconds),
        )
        return done(verdict, approved=target, approval_id=aid,
                    expires_at=s.approvals[aid].expires_at.isoformat(timespec="seconds"),
                    side=p.side, entry=float(p.entry), risk=risk_info, limits=lim_out, binding_limits=binding)

    # ------------------------------------------------------------------ public: bookkeeping

    def record_fill(self, *, approval_id: str, fill_price, contracts: int | None = None, stop=None) -> dict:
        with self.store.transaction() as s:
            self._housekeeping(s)
            ap = s.approvals.get(approval_id)
            if ap is None:
                raise RiskEngineError(f"approval '{approval_id}' not found or expired; run check_trade again")
            n = ap.contracts if contracts is None else int(contracts)
            if n <= 0:
                raise RiskEngineError("contracts must be positive")
            if n > ap.contracts:
                raise RiskEngineError(f"fill of {n} exceeds approved {ap.contracts} contracts")
            fill = to_dec(fill_price)
            inst = self.registry.get(ap.symbol)
            warnings: list[str] = []

            if ap.kind == "future":
                stop_p = to_dec(stop) if stop is not None else ap.stop_price
                if (ap.direction > 0 and stop_p >= fill) or (ap.direction < 0 and stop_p <= fill):
                    raise RiskEngineError(f"stop {stop_p} is on the wrong side of fill {fill}")
                stop_p = round_to_tick_away(stop_p, fill, inst.tick_size)
                bd = futures_risk_per_contract(inst, abs(fill - stop_p), ap.commission_rt, ap.slippage_ticks)
                rpc = bd["total"]
            else:
                stop_p = None
                struct = dict(ap.structure or {})
                key = "credit" if ap.direction < 0 else ("premium" if "premium" in struct else "debit")
                struct[key] = str(fill)
                comm_leg = self.rules.costs.commission_for(inst.symbol, "option")
                rpc = option_risk_per_unit(inst, struct, comm_leg).total

            approved_total = ap.risk_per_contract * n
            actual_total = rpc * n
            if actual_total > approved_total:
                warnings.append(f"Fill increased risk by ${money(actual_total - approved_total)} over the approval "
                                f"(${money(approved_total)} -> ${money(actual_total)}). Consider reducing size.")

            # The broker fill already happened, so it is always recorded. Limits are re-checked and any
            # breach is reported loudly instead of being hidden by a rejected bookkeeping call.
            del s.approvals[approval_id]
            breaches = self._fill_breaches(s, ap, actual_total)
            warnings.extend(f"BREACH {b['rule']}: {b['message']}" for b in breaches)

            pid = "pos_" + secrets.token_hex(5)
            pos = Position(
                id=pid, symbol=ap.symbol, kind=ap.kind, side=ap.side, direction=ap.direction, contracts=n,
                entry_price=fill, stop_price=stop_p, risk_per_contract=rpc, point_value=ap.point_value,
                commission_rt=ap.commission_rt, slippage_ticks=ap.slippage_ticks, tick_size=inst.tick_size,
                bucket=ap.bucket, margin_per_contract=ap.margin_per_contract, structure=ap.structure,
                approval_id=ap.id, opened_at=self._now(),
            )
            s.positions[pid] = pos
            s.log("fill", position_id=pid, approval_id=approval_id, symbol=ap.symbol, side=ap.side,
                  contracts=n, fill=str(fill), stop=None if stop_p is None else str(stop_p), risk=str(actual_total),
                  breaches=[b["rule"] for b in breaches])
            return {"position_id": pid, "symbol": ap.symbol, "side": ap.side, "contracts": n,
                    "entry_price": float(fill), "stop_price": None if stop_p is None else float(stop_p),
                    "open_risk": money(actual_total), "warnings": warnings, "breaches": breaches,
                    "account": self._context(s)}

    def _fill_breaches(self, s: RiskState, ap: Approval, new_risk: Decimal) -> list[dict]:
        """Limits that the position about to be opened would exceed (the approval is already removed)."""
        eq, pr, out = s.equity, self.rules.portfolio, []
        if s.day.locked:
            out.append({"rule": "daily_loss_lockout", "message": "day is locked; flatten or ask the owner to unlock"})
        if self.rules.drawdown_multiplier(self._drawdown_pct(s))[0] == 0:
            out.append({"rule": "drawdown_halt", "message": "trading is halted at the current drawdown"})
        if len(s.positions) + 1 > pr.max_open_positions:
            out.append({"rule": "max_open_positions", "message": f"exceeds {pr.max_open_positions} open positions"})
        if self._committed_risk(s) + new_risk > eq * pr.max_open_risk_pct / 100:
            out.append({"rule": "portfolio_heat", "message": f"total risk exceeds {pr.max_open_risk_pct}% of equity"})
        if self._committed_risk(s, ap.bucket) + new_risk > eq * pr.max_bucket_risk_pct / 100:
            out.append({"rule": "bucket_heat", "message": f"{ap.bucket} risk exceeds {pr.max_bucket_risk_pct}% of equity"})
        return out

    def cancel_approval(self, *, approval_id: str) -> dict:
        """Release the risk reserved by an approval that will not be used (order not placed or rejected)."""
        with self.store.transaction() as s:
            self._housekeeping(s)
            if s.approvals.pop(approval_id, None) is None:
                raise RiskEngineError(f"approval '{approval_id}' not found or already used/expired")
            s.log("cancel_approval", approval_id=approval_id)
            return {"cancelled": approval_id, "account": self._context(s)}

    def update_stop(self, *, position_id: str, new_stop) -> dict:
        with self.store.transaction() as s:
            self._housekeeping(s)
            pos = s.positions.get(position_id)
            if pos is None:
                raise RiskEngineError(f"position '{position_id}' not found")
            if pos.kind != "future":
                raise RiskEngineError("stops are only tracked for futures; option risk is the structure's max loss")
            steps = to_dec(new_stop) / pos.tick_size
            new = steps.to_integral_value(rounding=ROUND_FLOOR if pos.direction > 0 else ROUND_CEILING) * pos.tick_size
            old = pos.stop_price
            widening = (new < old) if pos.direction > 0 else (new > old)
            if widening and not self.rules.stops.allow_widening:
                s.log("update_stop_rejected", position_id=position_id, old=str(old), new=str(new))
                return {"verdict": REJECTED, "rule": "no_stop_widening",
                        "message": f"Moving the stop from {old} to {new} widens risk; the rulebook forbids it.",
                        "position_id": position_id, "stop_price": float(old)}
            price_pts = (pos.entry_price - new) * pos.direction
            price_risk = max(Decimal(0), price_pts * pos.point_value)
            rpc = price_risk + pos.slippage_ticks * pos.tick_size * pos.point_value + pos.commission_rt
            pos.stop_price, pos.risk_per_contract = new, rpc
            s.log("update_stop", position_id=position_id, old=str(old), new=str(new), risk=str(pos.open_risk))
            return {"verdict": APPROVED, "position_id": position_id, "stop_price": float(new),
                    "open_risk": money(pos.open_risk),
                    "locked_in_profit": money(max(Decimal(0), -price_pts) * pos.point_value * pos.contracts),
                    "account": self._context(s)}

    def record_close(self, *, position_id: str, exit_price, contracts: int | None = None) -> dict:
        with self.store.transaction() as s:
            self._housekeeping(s)
            pos = s.positions.get(position_id)
            if pos is None:
                raise RiskEngineError(f"position '{position_id}' not found")
            n = pos.contracts if contracts is None else int(contracts)
            if n <= 0 or n > pos.contracts:
                raise RiskEngineError(f"contracts must be between 1 and {pos.contracts}")
            exit_p = to_dec(exit_price)
            gross = (exit_p - pos.entry_price) * pos.direction * pos.point_value * n
            pnl = gross - pos.commission_rt * n

            s.equity += pnl
            s.peak_equity = max(s.peak_equity, s.equity)
            s.day.realized_pnl += pnl
            s.day.trades_closed += 1
            if n == pos.contracts:
                del s.positions[position_id]
            else:
                pos.contracts -= n

            events = []
            limit = self._daily_limit(s)
            if not s.day.locked and s.day.realized_pnl <= -limit:
                s.day.locked = True
                s.day.lock_reason = (f"daily realized loss ${money(-s.day.realized_pnl)} reached the "
                                     f"{self.rules.daily.max_daily_loss_pct}% limit")
                events.append("daily_loss_lockout")
            mult, tier = self.rules.drawdown_multiplier(self._drawdown_pct(s))
            if tier is not None:
                events.append("trading_halted" if mult == 0 else f"drawdown_tier_{tier.from_peak_pct}%")
            s.log("close", position_id=position_id, contracts=n, exit=str(exit_p), pnl=str(pnl),
                  equity=str(s.equity), events=events)
            return {"position_id": position_id, "closed_contracts": n, "exit_price": float(exit_p),
                    "gross_pnl": money(gross), "commission": money(pos.commission_rt * n), "net_pnl": money(pnl),
                    "remaining_contracts": 0 if position_id not in s.positions else s.positions[position_id].contracts,
                    "events": events, "account": self._context(s)}

    def risk_state(self) -> dict:
        s = self.store.read()
        self._housekeeping(s)
        return {
            "account": self._context(s),
            "positions": [{
                "position_id": p.id, "symbol": p.symbol, "side": p.side, "contracts": p.contracts,
                "entry_price": float(p.entry_price), "stop_price": None if p.stop_price is None else float(p.stop_price),
                "open_risk": money(p.open_risk), "bucket": p.bucket, "structure": p.structure,
                "opened_at": p.opened_at.isoformat(timespec="seconds"),
            } for p in s.positions.values()],
            "pending_approvals": [{
                "approval_id": a.id, "symbol": a.symbol, "side": a.side, "contracts": a.contracts,
                "expires_at": a.expires_at.isoformat(timespec="seconds"),
            } for a in s.approvals.values()],
            "recent_journal": s.journal[-10:],
            "rulebook": self.rules.summary(),
        }

    # ------------------------------------------------------------------ owner-only (admin CLI, not MCP)

    def admin_set_equity(self, amount, reset_peak: bool = False, note: str = "") -> dict:
        with self.store.transaction() as s:
            old = s.equity
            s.equity = to_dec(amount)
            if reset_peak or s.equity > s.peak_equity:
                s.peak_equity = s.equity
            s.log("admin_set_equity", old=str(old), new=str(s.equity), reset_peak=reset_peak, note=note)
            return self._context(s)

    def admin_reset_peak(self, note: str = "") -> dict:
        with self.store.transaction() as s:
            old = s.peak_equity
            s.peak_equity = s.equity
            s.log("admin_reset_peak", old=str(old), new=str(s.equity), note=note)
            return self._context(s)

    def admin_unlock_day(self, note: str = "") -> dict:
        with self.store.transaction() as s:
            self._housekeeping(s)
            s.day.locked, s.day.lock_reason = False, None
            s.log("admin_unlock_day", note=note)
            return self._context(s)

    def admin_ack_rulebook(self, note: str = "") -> dict:
        """Confirm the current rulebook is intended for this state (clears the rulebook_changed flag)."""
        with self.store.transaction() as s:
            old = s.created_with_rulebook
            s.created_with_rulebook = self.rules.fingerprint
            s.log("admin_ack_rulebook", old=old, new=self.rules.fingerprint, note=note)
            return self._context(s)

    def admin_remove_position(self, position_id: str, note: str = "") -> dict:
        """For positions closed outside the agent flow; does not touch P&L (use set-equity to sync)."""
        with self.store.transaction() as s:
            pos = s.positions.pop(position_id, None)
            if pos is None:
                raise RiskEngineError(f"position '{position_id}' not found")
            s.log("admin_remove_position", position_id=position_id, note=note)
            return self._context(s)
