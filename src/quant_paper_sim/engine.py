from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quant_paper_sim.models import Holding, PortfolioState, RebalanceResult, SignalBundle


def _lot_size(symbol: str) -> int:
    code = symbol.split(".")[0] if "." in symbol else symbol
    if code.startswith(("688", "689")):
        return 200
    return 100


def _floor_lots(shares: float, lot: int) -> int:
    if shares <= 0:
        return 0
    return int(shares // lot) * lot


def load_portfolio(path: Path, initial_capital: float) -> PortfolioState:
    if not path.is_file():
        return PortfolioState(as_of="", cash=initial_capital, holdings=[], initial_capital=initial_capital)
    data = json.loads(path.read_text(encoding="utf-8"))
    holdings = [
        Holding(symbol=h["symbol"], shares=int(h["shares"]), price=float(h["price"]))
        for h in data.get("holdings") or []
    ]
    return PortfolioState(
        as_of=str(data.get("as_of", "")),
        cash=float(data.get("cash", initial_capital)),
        holdings=holdings,
        initial_capital=float(data.get("initial_capital", initial_capital)),
    )


def save_portfolio(path: Path, portfolio: PortfolioState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(portfolio.to_dict(), indent=2), encoding="utf-8")


def append_nav(nav_path: Path, portfolio: PortfolioState) -> None:
    nav_path.parent.mkdir(parents=True, exist_ok=True)
    row = {"date": portfolio.as_of, "nav": portfolio.nav, "cash": portfolio.cash}
    if nav_path.is_file():
        df = pd.read_csv(nav_path)
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])
    df.to_csv(nav_path, index=False)


def write_holdings_csv(path: Path, portfolio: PortfolioState) -> None:
    if portfolio.nav <= 0:
        weights = []
    else:
        weights = [h.market_value / portfolio.nav for h in portfolio.holdings]
    rows = [
        {
            "symbol": h.symbol,
            "shares": h.shares,
            "price": h.price,
            "market_value": h.market_value,
            "weight": w,
        }
        for h, w in zip(portfolio.holdings, weights, strict=False)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def rebalance(
    portfolio: PortfolioState,
    signals: SignalBundle,
    *,
    commission_rate: float = 0.0003,
) -> RebalanceResult:
    nav = portfolio.nav if portfolio.nav > 0 else portfolio.initial_capital or portfolio.cash
    investable = nav * (1.0 - signals.cash_reserve) * signals.regime_scale
    targets = signals.targets
    if not targets:
        raise ValueError("signal targets are empty")

    total_w = sum(t.weight for t in targets)
    if total_w <= 0:
        raise ValueError("target weights must sum to a positive value")

    desired: dict[str, Holding] = {}
    trades: list[dict] = []
    spent = 0.0

    for t in targets:
        norm_w = t.weight / total_w
        budget = investable * norm_w
        lot = _lot_size(t.symbol)
        shares = _floor_lots(budget / t.price, lot)
        if shares <= 0:
            continue
        cost = shares * t.price
        fee = cost * commission_rate
        spent += cost + fee
        desired[t.symbol] = Holding(symbol=t.symbol, shares=shares, price=t.price)
        trades.append(
            {
                "symbol": t.symbol,
                "side": "buy",
                "shares": shares,
                "price": t.price,
                "fee": round(fee, 4),
            }
        )

    cash = max(nav - spent, 0.0)
    new_portfolio = PortfolioState(
        as_of=signals.as_of,
        cash=round(cash, 2),
        holdings=sorted(desired.values(), key=lambda h: h.symbol),
        initial_capital=portfolio.initial_capital or nav,
    )
    return RebalanceResult(portfolio=new_portfolio, trades=trades)


def run_step(config_path: Path) -> RebalanceResult:
    from quant_paper_sim.readers.signals import load_config, load_signals

    cfg = load_config(config_path)
    repo_root = config_path.parent.parent
    state_dir = Path(cfg.get("state_dir", "state"))
    if not state_dir.is_absolute():
        state_dir = repo_root / state_dir

    initial_capital = float(cfg.get("initial_capital", 100_000))
    portfolio_path = state_dir / "portfolio.json"
    nav_path = state_dir / "nav.csv"
    holdings_path = state_dir / "holdings.csv"

    portfolio = load_portfolio(portfolio_path, initial_capital)
    if portfolio.initial_capital <= 0:
        portfolio.initial_capital = initial_capital
    if portfolio.cash <= 0 and not portfolio.holdings:
        portfolio.cash = initial_capital

    signals = load_signals(cfg, repo_root)
    commission = float(cfg.get("commission_rate", 0.0003))
    result = rebalance(portfolio, signals, commission_rate=commission)

    save_portfolio(portfolio_path, result.portfolio)
    append_nav(nav_path, result.portfolio)
    write_holdings_csv(holdings_path, result.portfolio)

    trades_path = state_dir / "trades.json"
    trades_path.write_text(json.dumps(result.trades, indent=2), encoding="utf-8")
    return result
