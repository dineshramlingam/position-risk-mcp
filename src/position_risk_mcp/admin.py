"""Owner-only CLI. These actions are deliberately NOT exposed as MCP tools.

    position-risk-admin show
    position-risk-admin set-equity 55000 [--reset-peak] [--note "synced with broker"]
    position-risk-admin reset-peak --note "reviewed drawdown, resuming"
    position-risk-admin unlock-day --note "..."
    position-risk-admin ack-rulebook --note "edited rules.yaml on purpose"
    position-risk-admin remove-position pos_abc123 --note "closed manually at broker"
    position-risk-admin size ES long 6500 6488 [--no-costs] [--risk-pct 1]
"""
from __future__ import annotations

import argparse
import json

from .config import build_engine


def main() -> None:
    ap = argparse.ArgumentParser(prog="position-risk-admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    p = sub.add_parser("set-equity"); p.add_argument("amount", type=float)
    p.add_argument("--reset-peak", action="store_true"); p.add_argument("--note", default="")
    p = sub.add_parser("reset-peak"); p.add_argument("--note", default="")
    p = sub.add_parser("unlock-day"); p.add_argument("--note", default="")
    p = sub.add_parser("ack-rulebook"); p.add_argument("--note", default="")
    p = sub.add_parser("remove-position"); p.add_argument("position_id"); p.add_argument("--note", default="")
    p = sub.add_parser("size"); p.add_argument("symbol"); p.add_argument("side", choices=["long", "short"])
    p.add_argument("entry", type=float); p.add_argument("stop", type=float)
    p.add_argument("--risk-pct", type=float); p.add_argument("--no-costs", action="store_true")
    a = ap.parse_args()

    e = build_engine()
    if a.cmd == "show":
        out = e.risk_state()
    elif a.cmd == "set-equity":
        out = e.admin_set_equity(a.amount, a.reset_peak, a.note)
    elif a.cmd == "reset-peak":
        out = e.admin_reset_peak(a.note)
    elif a.cmd == "unlock-day":
        out = e.admin_unlock_day(a.note)
    elif a.cmd == "ack-rulebook":
        out = e.admin_ack_rulebook(a.note)
    elif a.cmd == "remove-position":
        out = e.admin_remove_position(a.position_id, a.note)
    else:
        out = e.calculate_position_size(symbol=a.symbol, side=a.side, entry=a.entry, stop=a.stop,
                                        risk_pct=a.risk_pct, include_costs=not a.no_costs)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
