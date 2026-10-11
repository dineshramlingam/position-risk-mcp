# trading-position-risk-mcp

[![PyPI](https://img.shields.io/pypi/v/trading-position-risk-mcp)](https://pypi.org/project/trading-position-risk-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/trading-position-risk-mcp)](https://pypi.org/project/trading-position-risk-mcp/)
[![CI](https://github.com/dineshrm15/trading-position-risk-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/dineshrm15/trading-position-risk-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **Not financial advice. Use at your own risk.** This is a guardrail that enforces rules *you* configure.
> It cannot see your broker, does not place orders, and trusts what the agent reports. It can be wrong,
> and trading can lose money. Test with paper trading first. See [Security and trust model](#security-and-trust-model).

A deterministic position-sizing and pre-trade risk server for trading agents, served over MCP.

Agents never do sizing math themselves. They ask this server. It applies a rulebook that the human
owns, and it answers **APPROVED / RESIZED / REJECTED** with the rules that fired and an approval token.

The core formula reproduces the *E-Mini (ES) Position Sizing Calculator v4.5* spreadsheet exactly
(`floor(equity × risk% ÷ (stop points × point value))`). On top of that it adds costs, micro
fallback, portfolio heat, exposure buckets, a daily-loss lockout, drawdown tiers and options.

## Quick start

Requires **Python 3.11+**. The easiest way to run the commands is `uvx` or `pipx`:

```bash
uvx --python 3.11 --from trading-position-risk-mcp position-risk-admin show
```

First run: the server starts with the `starting_equity` in `rules.yaml` ($55,000 by default). Enter your
real account size once, before any agent trades:

```bash
position-risk-admin set-equity 25000 --note "initial"
```

```bash
pip install -e ".[dev]"
pytest                                   # 190+ tests, including spreadsheet parity
position-risk-mcp                        # stdio MCP server
position-risk-mcp --http 8765            # streamable HTTP instead (loopback only; see Security)
position-risk-admin size ES long 6500 6498 --no-costs    # same answer as the sheet: 5 contracts
```

Claude Desktop / Claude Code config:

```json
{
  "mcpServers": {
    "position-risk": {
      "command": "position-risk-mcp",
      "env": {
        "POSITION_RISK_RULES": "/path/to/config/rules.yaml",
        "POSITION_RISK_STATE": "~/.position-risk-mcp/risk_state.json"
      }
    }
  }
}
```

| Env var | Default |
|---|---|
| `POSITION_RISK_RULES` | `./config/rules.yaml`, then the repo's `config/` |
| `POSITION_RISK_INSTRUMENTS` | `./config/instruments.yaml`, then the repo's `config/` |
| `POSITION_RISK_STATE` | `~/.position-risk-mcp/risk_state.json` |

## Order flow the agent must follow

```
check_trade ──► place order (≤ approved_contracts, before expiry) ──► record_fill ──► update_stop* ──► record_close
```

| Tool | Purpose |
|---|---|
| `check_trade` | **The gate.** Applies every limit; returns verdict, `approved_contracts`, `approval_id`, binding limits, and a micro suggestion when the full size rounds to 0. |
| `calculate_position_size` | Read-only sizing for one trade (ignores other open positions). `include_costs=false` reproduces the spreadsheet. |
| `record_fill` | Opens a tracked position against an approval. It refuses quantities above the approval or approvals that have expired. The fill is always recorded (it already happened at the broker), but limits are re-checked and any `breaches` are returned. If slippage raised the risk, it warns. |
| `cancel_approval` | Releases the risk reserved by an approval you won't use. |
| `update_stop` | Moves a stop closer or trails it into profit. Widening is rejected. |
| `record_close` | Realizes P&L and updates equity, peak, daily P&L, lockouts and drawdown tiers. Supports partial closes. |
| `get_risk_state` | Equity, drawdown, multiplier, daily budget, positions, bucket exposure, pending approvals, journal. |
| `get_instrument_spec` / `list_instruments` / `get_rulebook` | Reference data. |

**By design, no tool can change equity, rules or lockouts.** Those are owner-only, through the admin CLI:

```bash
position-risk-admin show
position-risk-admin set-equity 61250 --note "synced with broker"
position-risk-admin reset-peak --note "reviewed drawdown, resuming"
position-risk-admin unlock-day --note "..."
position-risk-admin remove-position pos_ab12cd34ef --note "closed manually"
```

## What `check_trade` enforces (config/rules.yaml)

| Rule | Default | Effect |
|---|---|---|
| Per-trade risk | 1% default, 2% max | Larger requests are capped (RESIZED) |
| Costs | commission + 1 tick stop slippage | Added to risk per contract |
| Portfolio heat | 5% of equity | Total open risk across all positions |
| Bucket heat | 3% of equity | ES, MES, NQ, MNQ, RTY, YM, SPX, XSP, SPY and QQQ all count as one `us_equity_index` exposure |
| Daily loss | 3% of start-of-day equity | Hard lockout until the next trading day. A new trade's full stop-out must also fit in what's left of the day's budget |
| Drawdown tiers | −5% from peak → ½ size; −10% → halt | Halt lasts until the owner runs `reset-peak` |
| Open positions | 4 | |
| Margin | 50% utilization | Only for instruments where you set `margin_per_contract` |
| Contract cap | ES/NQ 20, others 100 | |
| Options | defined risk only | Sized on max loss; naked, short straddle, short strangle and ratio structures are rejected |
| Stops | snapped to tick, away from entry | Never widened |

Order of evaluation: lockout → drawdown halt → position count → order validity → min(per-trade,
heat, bucket, daily budget, margin, cap). The smallest limit wins and is reported in `binding_limits`.

## Examples (with the default $55,000 account)

| Trade | Result |
|---|---|
| ES long 6500, stop 6498, costs off | 5 contracts (matches the spreadsheet) |
| ES long 6500, stop 6498, costs on | 4 contracts ($116.50 risk each) |
| ES long 6500, stop 6485 | REJECTED → suggestion: **7 MES** |
| SPX credit spread, width 5, credit 1.20 | 1 spread ($382.60 max loss) |
| 3rd MES position, then an NQ trade | NQ REJECTED by `bucket_heat` → suggests MNQ |

## Security and trust model

- **Pending approvals reserve risk.** Until filled, cancelled or expired, an approval counts against heat,
  bucket, daily-budget, margin and position-count limits. A new `check_trade` for a symbol replaces that
  symbol's earlier pending approval.
- **The agent reports its own fills and closes.** The server cannot see your broker. An agent that skips
  `record_close` or reports a false `exit_price` makes equity, lockouts and drawdown tiers wrong. Treat the
  server as a guardrail for a cooperative agent, not as enforcement, and reconcile with your broker using
  `position-risk-admin set-equity` / `remove-position`.
- **HTTP transport.** It binds to loopback by default. Any other `--host` is refused unless
  `POSITION_RISK_TOKEN` (24+ characters) is set; clients then send `Authorization: Bearer <token>`.
  There is no TLS: put a reverse proxy in front for anything beyond localhost. Every tool is available to
  any authenticated client.
- **Rulebook edits.** State remembers which rulebook fingerprint created it. If `rules.yaml` changes,
  `rulebook_changed` is true in every response until the owner runs `position-risk-admin ack-rulebook`.

## Design notes

- **Decimal math throughout,** so `floor()` never turns 11.0 into 10.999.
- **Equity is never an input.** It starts at `account.starting_equity`, moves with realized P&L and
  can be synced with `set-equity`. A hallucinated account size can't inflate a position.
- **Every response carries the rulebook version and fingerprint,** and every state change is
  journaled, so you can audit which rules approved which trade.
- **The state store is pluggable.** `JsonFileStore` (file lock + atomic write) is the default.
  For AWS, implement the same `transaction()` / `read()` interface on DynamoDB with a conditional
  version write, and run the server behind Lambda/Fargate with `--http`.
- **Margins in `instruments.yaml` are null on purpose.** They change, and they differ by broker
  and session, so set them from your broker.

## Roadmap ideas

- Broker adapters (equity and positions pulled from the broker instead of tracked locally)
- DynamoDB store + Lambda handler
- Futures options; per-symbol session calendars for the day rollover
- Volatility-regime multiplier (e.g. from a VIX regime feed)

## License

[MIT](LICENSE).

*This software enforces rules you configure. It is not financial advice.*
