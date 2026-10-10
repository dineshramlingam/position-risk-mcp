from decimal import Decimal

import pytest

from trading_position_risk_mcp.engine import APPROVED, REJECTED, RESIZED, RiskEngineError
from trading_position_risk_mcp.sizing import spreadsheet_size


# ---------------------------------------------------------------- sizing basics

def test_decimal_floor_is_exact():
    # float math gives 0.3/0.1 = 2.9999999 -> 2; Decimal gives 3
    assert spreadsheet_size(30, "0.1", 1, 1) == 3


def test_symbol_normalization(registry):
    assert registry.get("/es").symbol == "ES"
    assert registry.get("ESZ6").symbol == "ES"
    assert registry.get("MESH27").symbol == "MES"
    assert registry.get("mnq").symbol == "MNQ"


def test_costs_included_by_default(engine):
    r = engine.calculate_position_size(symbol="ES", side="long", entry=6500, stop=6498)
    # 2 pts * 50 + 1 tick slippage 12.50 + 4.00 commission = 116.50 ; 550 / 116.5 = 4.72
    assert r["risk_per_contract"] == 116.5
    assert r["contracts"] == 4


def test_micro_alternative_when_es_rounds_to_zero(engine):
    r = engine.calculate_position_size(symbol="ES", side="long", entry=6500, stop=6485)
    assert r["contracts"] == 0
    # MES: 15 * 5 + 1.25 + 1.50 = 77.75 ; 550 / 77.75 = 7.07
    assert r["micro_alternative"]["symbol"] == "MES"
    assert r["micro_alternative"]["contracts"] == 7


def test_atr_stop(engine):
    r = engine.calculate_position_size(symbol="MES", side="short", entry=6500, atr=6, atr_multiple=2)
    assert r["stop_price"] == 6512 and r["method"] == "atr"


def test_stop_snapped_away_from_entry(engine):
    r = engine.calculate_position_size(symbol="ES", side="long", entry=6500, stop=6488.1)
    assert r["stop_price"] == 6488.0


def test_risk_pct_capped(engine):
    r = engine.calculate_position_size(symbol="MES", side="long", entry=6500, stop=6490, risk_pct=5)
    assert r["risk_pct_effective"] == 2.0


# ---------------------------------------------------------------- the gate

def test_check_trade_resizes_oversized_request(engine):
    r = engine.check_trade(symbol="ES", side="long", entry=6500, stop=6498, contracts=10)
    assert r["verdict"] == RESIZED and r["approved_contracts"] == 4
    assert "per_trade_risk" in r["binding_limits"]
    assert r["approval_id"].startswith("apr_")


def test_check_trade_approves_within_limits(engine):
    r = engine.check_trade(symbol="ES", side="long", entry=6500, stop=6498, contracts=2)
    assert r["verdict"] == APPROVED and r["approved_contracts"] == 2


def test_check_trade_rejects_and_suggests_micro(engine):
    r = engine.check_trade(symbol="ES", side="long", entry=6500, stop=6485)
    assert r["verdict"] == REJECTED and "approval_id" not in r
    assert r["suggestion"]["symbol"] == "MES" and r["suggestion"]["contracts"] == 7


def test_wrong_side_stop_rejected(engine):
    r = engine.check_trade(symbol="ES", side="long", entry=6500, stop=6510)
    assert r["verdict"] == REJECTED and r["reasons"][0]["rule"] == "invalid_order"


def test_missing_stop_rejected(engine):
    r = engine.check_trade(symbol="ES", side="long", entry=6500)
    assert r["verdict"] == REJECTED


def test_unknown_symbol(engine):
    assert engine.check_trade(symbol="DOGE", side="long", entry=1, stop=0.5)["reasons"][0]["rule"] == "unknown_instrument"


def _open(engine, symbol="MES", side="long", entry=6500, stop=6490, contracts=None, **kw):
    c = engine.check_trade(symbol=symbol, side=side, entry=entry, stop=stop, contracts=contracts, **kw)
    assert c["verdict"] in (APPROVED, RESIZED), c
    return engine.record_fill(approval_id=c["approval_id"], fill_price=entry)


def test_bucket_heat_limits_stacking(engine):
    # Each MES trade at 10 pt stop: 50 + 1.25 + 1.5 = 52.75/contract, 10 contracts = $527.50 (~0.96%)
    _open(engine, contracts=10)
    _open(engine, contracts=10)
    _open(engine, contracts=10)   # bucket now ~$1582.50 of $1650 (3%)
    r = engine.check_trade(symbol="NQ", side="long", entry=24000, stop=23990, contracts=1)
    # NQ is in the same bucket; 10 pts * 20 + 5 + 4 = 209 > remaining ~$67
    assert r["verdict"] == REJECTED
    assert "bucket_heat" in r["binding_limits"]
    assert r["suggestion"]["symbol"] == "MNQ"


def test_max_open_positions(engine):
    for _ in range(4):
        _open(engine, contracts=1, stop=6495)
    r = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6495, contracts=1)
    assert r["verdict"] == REJECTED and r["reasons"][0]["rule"] == "max_open_positions"


def test_daily_loss_lockout(engine):
    # Lose > 3% of equity in realized P&L
    p = _open(engine, contracts=10)                    # MES long 6500, stop 6490
    engine.record_close(position_id=p["position_id"], exit_price=6490)   # -500 - 15
    p = _open(engine, contracts=10, entry=6480, stop=6470)
    engine.record_close(position_id=p["position_id"], exit_price=6470)
    p = _open(engine, contracts=10, entry=6460, stop=6450)
    out = engine.record_close(position_id=p["position_id"], exit_price=6430)  # stopped badly
    assert "daily_loss_lockout" in out["events"]
    r = engine.check_trade(symbol="MES", side="long", entry=6400, stop=6395, contracts=1)
    assert r["verdict"] == REJECTED and r["reasons"][0]["rule"] == "daily_loss_lockout"


def test_lockout_clears_next_day(engine, clock):
    engine.admin_set_equity(55000)
    with engine.store.transaction() as s:
        s.day.locked, s.day.lock_reason = True, "test"
    clock.advance(days=1)
    r = engine.check_trade(symbol="MES", side="long", entry=6400, stop=6395, contracts=1)
    assert r["verdict"] == APPROVED


def test_daily_budget_reserves_open_risk(engine):
    # Realized -1300 today, limit 1650 => only ~350 of fresh risk allowed
    with engine.store.transaction() as s:
        s.day.realized_pnl = Decimal(-1300)
    r = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490)
    assert r["limits"]["daily_loss_budget"]["max"] == 6   # floor(350 / 52.75)
    assert r["approved_contracts"] == 6


def test_drawdown_tier_halves_size(engine):
    engine.admin_set_equity(55000, reset_peak=True)
    engine.admin_set_equity(52000)                      # -5.45% from peak
    r = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490)
    # 52000 * 1% * 0.5 = 260 ; 260 / 52.75 = 4.9
    assert r["approved_contracts"] == 4
    assert any(x["rule"] == "drawdown_reduction" for x in r["reasons"])


def test_drawdown_halt(engine):
    engine.admin_set_equity(55000, reset_peak=True)
    engine.admin_set_equity(49000)                      # -10.9%
    r = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=1)
    assert r["verdict"] == REJECTED and r["reasons"][0]["rule"] == "drawdown_halt"
    engine.admin_reset_peak(note="reviewed")
    assert engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=1)["verdict"] == APPROVED


# ---------------------------------------------------------------- options

def test_credit_spread_sized_on_max_loss(engine):
    r = engine.check_trade(symbol="SPX", structure={"type": "credit_spread", "width": 5, "credit": 1.2})
    # (5 - 1.2) * 100 + 2 legs * 1.30 = 382.60 ; 550 / 382.6 = 1.4
    assert r["risk"]["risk_per_contract"] == 382.6 and r["approved_contracts"] == 1
    assert r["side"] == "short"


def test_long_call(engine):
    r = engine.calculate_position_size(symbol="SPX", structure={"type": "long_call", "premium": 2.0})
    assert r["contracts"] == 2   # 201.30 each


def test_undefined_risk_rejected(engine):
    r = engine.check_trade(symbol="SPX", structure={"type": "short_strangle", "credit": 4})
    assert r["verdict"] == REJECTED and r["reasons"][0]["rule"] == "undefined_risk"


def test_credit_spread_pnl(engine):
    c = engine.check_trade(symbol="SPX", structure={"type": "credit_spread", "width": 5, "credit": 1.2})
    f = engine.record_fill(approval_id=c["approval_id"], fill_price=1.25)   # filled better
    assert f["open_risk"] == 377.6
    out = engine.record_close(position_id=f["position_id"], exit_price=0.25)
    assert out["net_pnl"] == 100 - 2.6


# ---------------------------------------------------------------- lifecycle

def test_fill_cannot_exceed_approval(engine):
    c = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=3)
    with pytest.raises(RiskEngineError):
        engine.record_fill(approval_id=c["approval_id"], fill_price=6500, contracts=4)


def test_approval_expires(engine, clock):
    c = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=3)
    clock.advance(seconds=301)
    with pytest.raises(RiskEngineError):
        engine.record_fill(approval_id=c["approval_id"], fill_price=6500)


def test_fill_slippage_warns(engine):
    c = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=3)
    f = engine.record_fill(approval_id=c["approval_id"], fill_price=6502)
    assert f["warnings"] and "increased risk" in f["warnings"][0]


def test_stop_cannot_widen_but_can_trail(engine):
    p = _open(engine, symbol="ES", entry=6500, stop=6496, contracts=2)
    r = engine.update_stop(position_id=p["position_id"], new_stop=6490)
    assert r["verdict"] == REJECTED and r["rule"] == "no_stop_widening"
    r = engine.update_stop(position_id=p["position_id"], new_stop=6498)
    assert r["verdict"] == APPROVED and r["open_risk"] == (2 * 50 + 12.5 + 4) * 2
    r = engine.update_stop(position_id=p["position_id"], new_stop=6505)   # trailed into profit
    assert r["open_risk"] == (12.5 + 4) * 2 and r["locked_in_profit"] == 500


def test_futures_pnl_long_and_short(engine):
    p = _open(engine, symbol="ES", entry=6500, stop=6496, contracts=2)
    out = engine.record_close(position_id=p["position_id"], exit_price=6510)
    assert out["net_pnl"] == 10 * 50 * 2 - 8
    p = _open(engine, symbol="ES", side="short", entry=6510, stop=6514, contracts=1)
    out = engine.record_close(position_id=p["position_id"], exit_price=6500)
    assert out["net_pnl"] == 500 - 4
    assert engine.risk_state()["account"]["equity"] == 55000 + 992 + 496


def test_partial_close(engine):
    p = _open(engine, symbol="MES", entry=6500, stop=6490, contracts=4)
    out = engine.record_close(position_id=p["position_id"], exit_price=6505, contracts=1)
    assert out["remaining_contracts"] == 3


def test_state_persists_across_engine_instances(engine, rules, registry, clock):
    from trading_position_risk_mcp.engine import RiskEngine
    _open(engine, contracts=2)
    e2 = RiskEngine(rules, registry, store=engine.store, now=clock)
    assert len(e2.risk_state()["positions"]) == 1


# ---------------------------------------------------------------- pending approvals reserve risk

def test_pending_approvals_reserve_heat(engine):
    ids = []
    for sym in ("ES", "NQ", "MES", "MNQ"):
        r = engine.check_trade(symbol=sym, side="long", entry=6500 if "ES" in sym else 25000,
                               stop=6490 if "ES" in sym else 24950, contracts=50)
        ids.append(r.get("approval_id"))
    state = engine.risk_state()
    # whatever was approved, reserved risk can never exceed the portfolio heat cap
    assert state["account"]["pending_approval_risk"] <= 55000 * 0.05 + 0.01


def test_pending_approvals_count_toward_position_cap(engine):
    for sym in ("ES", "NQ", "RTY", "YM"):
        r = engine.check_trade(symbol=sym, side="long", entry=1000, stop=999, contracts=1)
        assert r["verdict"] != REJECTED, (sym, r["reasons"])
    r = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=1)
    assert r["verdict"] == REJECTED and r["reasons"][0]["rule"] == "max_open_positions"


def test_recheck_supersedes_same_symbol(engine):
    a = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=2)
    b = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=2)
    assert b["verdict"] == APPROVED and b["approved_contracts"] == 2
    assert len(engine.risk_state()["pending_approvals"]) == 1
    with pytest.raises(RiskEngineError):
        engine.record_fill(approval_id=a["approval_id"], fill_price=6500)


def test_cancel_approval_releases_risk(engine):
    a = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=2)
    assert engine.risk_state()["account"]["pending_approval_risk"] > 0
    engine.cancel_approval(approval_id=a["approval_id"])
    assert engine.risk_state()["account"]["pending_approval_risk"] == 0
    with pytest.raises(RiskEngineError):
        engine.cancel_approval(approval_id=a["approval_id"])


def test_fill_reports_breaches_when_locked_after_approval(engine):
    a = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=1)
    with engine.store.transaction() as s:
        s.day.locked, s.day.lock_reason = True, "test"
    f = engine.record_fill(approval_id=a["approval_id"], fill_price=6500)
    assert f["position_id"] and [b["rule"] for b in f["breaches"]] == ["daily_loss_lockout"]


def test_clean_fill_has_no_breaches(engine):
    a = engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490, contracts=1)
    assert engine.record_fill(approval_id=a["approval_id"], fill_price=6500)["breaches"] == []


def test_stop_update_rounds_conservatively(engine):
    a = engine.check_trade(symbol="ES", side="long", entry=6500, stop=6490, contracts=1)
    pid = engine.record_fill(approval_id=a["approval_id"], fill_price=6500)["position_id"]
    r = engine.update_stop(position_id=pid, new_stop=6495.2)   # tick 0.25 -> floor for a long
    assert r["stop_price"] == 6495.0


def test_rulebook_change_flagged_until_acked(engine, rules):
    engine.check_trade(symbol="MES", side="long", entry=6500, stop=6490)   # state created
    assert engine.risk_state()["account"]["rulebook_changed"] is False
    engine.rules.fingerprint = "changed"
    assert engine.risk_state()["account"]["rulebook_changed"] is True
    engine.admin_ack_rulebook()
    assert engine.risk_state()["account"]["rulebook_changed"] is False
