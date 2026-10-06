from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml
from quant_execution import ReplayError

from quant_paper_sim.engine import initialize, preflight, run_step, status
from quant_paper_sim.recovery import backup_state, restore_state, verify_state
from quant_paper_sim.state import StateError


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Paper trading simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("step", "Load research-close signals and execute a paper rebalance"),
        ("init", "Reset authoritative paper execution state to cash"),
        ("status", "Replay authoritative state and print its portfolio projection"),
        ("preflight", "Read and validate inputs without replaying or modifying the account"),
        ("verify", "Replay and inspect saved account evidence without modifying files"),
        ("backup", "Create an independent verified authoritative journal backup"),
        ("restore", "Restore a verified backup to an empty state directory"),
    ):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--config", required=True)
        if command == "backup":
            item.add_argument("--out", required=True, type=Path)
        elif command == "restore":
            item.add_argument("--backup", required=True, type=Path)

    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        if args.command in {"verify", "backup", "restore"}:
            if args.command == "backup":
                evidence = backup_state(config_path, args.out)
            elif args.command == "restore":
                evidence = restore_state(config_path, args.backup)
            else:
                evidence = verify_state(config_path)
            print(json.dumps(evidence, indent=2, ensure_ascii=False))
            return
        if args.command == "preflight":
            print(json.dumps(preflight(config_path), indent=2, ensure_ascii=False))
            return
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
    except (StateError, ReplayError, ValueError, OSError, yaml.YAMLError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
