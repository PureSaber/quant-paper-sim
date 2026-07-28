from __future__ import annotations

import argparse
import json
from pathlib import Path

from quant_paper_sim.engine import load_portfolio, run_step, save_portfolio
from quant_paper_sim.readers.signals import load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Paper trading simulator")
    sub = parser.add_subparsers(dest="command", required=True)

    step = sub.add_parser("step", help="Load signals and rebalance paper portfolio")
    step.add_argument("--config", required=True)

    init = sub.add_parser("init", help="Reset paper portfolio to cash")
    init.add_argument("--config", required=True)

    status = sub.add_parser("status", help="Print current portfolio JSON")
    status.add_argument("--config", required=True)

    args = parser.parse_args(argv)
    config_path = Path(args.config)

    if args.command == "step":
        result = run_step(config_path)
        print(
            f"paper step as_of={result.portfolio.as_of} nav={result.portfolio.nav:.2f} "
            f"holdings={len(result.portfolio.holdings)} trades={len(result.trades)}"
        )
        return

    cfg = load_config(config_path)
    repo_root = config_path.parent.parent
    state_dir = Path(cfg.get("state_dir", "state"))
    if not state_dir.is_absolute():
        state_dir = repo_root / state_dir
    initial_capital = float(cfg.get("initial_capital", 100_000))
    portfolio_path = state_dir / "portfolio.json"

    if args.command == "init":
        portfolio = load_portfolio(Path("__missing__"), initial_capital)
        portfolio.cash = initial_capital
        portfolio.as_of = "init"
        save_portfolio(portfolio_path, portfolio)
        print(f"initialized paper portfolio cash={initial_capital}")
        return

    if args.command == "status":
        portfolio = load_portfolio(portfolio_path, initial_capital)
        print(json.dumps(portfolio.to_dict(), indent=2))


if __name__ == "__main__":
    main()
