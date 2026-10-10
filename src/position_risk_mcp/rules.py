"""Rulebook: validated, read-only risk configuration owned by the human."""
from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountRules(_Base):
    starting_equity: Decimal = Field(gt=0)
    timezone: str = "America/New_York"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v


class PerTradeRules(_Base):
    default_risk_pct: Decimal = Field(default=Decimal("1.0"), gt=0)
    max_risk_pct: Decimal = Field(default=Decimal("2.0"), gt=0, le=10)

    @model_validator(mode="after")
    def _order(self):
        if self.default_risk_pct > self.max_risk_pct:
            raise ValueError("default_risk_pct must be <= max_risk_pct")
        return self


class PortfolioRules(_Base):
    max_open_risk_pct: Decimal = Field(default=Decimal("5"), gt=0)
    max_bucket_risk_pct: Decimal = Field(default=Decimal("3"), gt=0)
    max_open_positions: int = Field(default=4, ge=1)
    max_margin_utilization_pct: Decimal | None = Field(default=Decimal("50"), gt=0, le=100)


class DailyRules(_Base):
    max_daily_loss_pct: Decimal = Field(default=Decimal("3"), gt=0)
    reserve_open_risk: bool = True


class DrawdownTier(_Base):
    from_peak_pct: Decimal = Field(gt=0)
    size_multiplier: Decimal = Field(ge=0, le=1)


class CostRules(_Base):
    commission_round_turn: dict[str, Decimal] = Field(default_factory=lambda: {"default": Decimal("4.00")})
    slippage_ticks: int = Field(default=1, ge=0)

    def commission_for(self, symbol: str, kind: str) -> Decimal:
        c = self.commission_round_turn
        if symbol in c:
            return c[symbol]
        if kind == "option" and "options" in c:
            return c["options"]
        return c.get("default", Decimal("0"))


class StopRules(_Base):
    allow_widening: bool = False


class ApprovalRules(_Base):
    ttl_seconds: int = Field(default=300, ge=10)


class Rulebook(_Base):
    version: str
    account: AccountRules
    per_trade: PerTradeRules = PerTradeRules()
    portfolio: PortfolioRules = PortfolioRules()
    daily: DailyRules = DailyRules()
    drawdown_tiers: list[DrawdownTier] = Field(default_factory=list)
    costs: CostRules = CostRules()
    stops: StopRules = StopRules()
    approvals: ApprovalRules = ApprovalRules()
    max_contracts: dict[str, int] = Field(default_factory=lambda: {"default": 100})
    fingerprint: str = ""

    @field_validator("drawdown_tiers")
    @classmethod
    def _sorted(cls, tiers: list[DrawdownTier]) -> list[DrawdownTier]:
        tiers = sorted(tiers, key=lambda t: t.from_peak_pct)
        mults = [t.size_multiplier for t in tiers]
        if mults != sorted(mults, reverse=True):
            raise ValueError("drawdown tier multipliers must not increase as drawdown deepens")
        return tiers

    def max_contracts_for(self, symbol: str) -> int:
        return self.max_contracts.get(symbol, self.max_contracts.get("default", 100))

    def drawdown_multiplier(self, drawdown_pct: Decimal) -> tuple[Decimal, DrawdownTier | None]:
        mult, active = Decimal("1"), None
        for tier in self.drawdown_tiers:
            if drawdown_pct >= tier.from_peak_pct:
                mult, active = tier.size_multiplier, tier
        return mult, active

    def summary(self) -> dict:
        return {"version": self.version, "fingerprint": self.fingerprint}


def load_rulebook(path: str | Path) -> Rulebook:
    text = Path(path).read_text()
    data = yaml.safe_load(text) or {}
    rb = Rulebook.model_validate(data)
    rb.fingerprint = hashlib.sha256(text.encode()).hexdigest()[:12]
    return rb
