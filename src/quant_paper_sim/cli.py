from __future__ import annotations

import argparse
import json
from pathlib import Path

from quant_execution import ReplayError

from quant_paper_sim.engine import initialize, run_step, status
from quant_paper_sim.state import StateError


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Paper trading simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("step", "Load research-close signals and execute a paper rebalance"),
        ("init", "Reset authoritative paper execution state to cash"),
        ("status", "Replay authoritative state and print its portfolio projection"),
    ):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--config", required=True)

    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        if args.command == "init":
            result = initialize(config_path)
            print(f"initialized authoritative paper ledger cash={result.portfolio.cash:.2f}")
            return
        if args.command == "step":
            result = run_step(config_path)
            print(
                f"paper step as_of={result.portfolio.as_of} nav={result.portfolio.nav:.2f} "
                f"holdings={len(result.portfolio.holdings)} fills={len(result.trades)}"
            )
            return
        portfolio = status(config_path)
        print(json.dumps(portfolio.to_dict(), indent=2, ensure_ascii=False))
    except (StateError, ReplayError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
