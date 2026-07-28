from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TargetPosition:
    symbol: str
    weight: float
    price: float


@dataclass
class SignalBundle:
    as_of: str
    targets: list[TargetPosition]
    regime_scale: float = 1.0
    cash_reserve: float = 0.05


@dataclass
class Holding:
    symbol: str
    shares: int
    price: float

    @property
    def market_value(self) -> float:
        return self.shares * self.price


@dataclass
class PortfolioState:
    as_of: str
    cash: float
    holdings: list[Holding] = field(default_factory=list)
    initial_capital: float = 0.0

    @property
    def invested_value(self) -> float:
        return sum(h.market_value for h in self.holdings)

    @property
    def nav(self) -> float:
        return self.cash + self.invested_value

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["nav"] = self.nav
        payload["invested_value"] = self.invested_value
        return payload


@dataclass
class RebalanceResult:
    portfolio: PortfolioState
    trades: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio": self.portfolio.to_dict(),
            "trades": self.trades,
            "nav": self.portfolio.nav,
        }
