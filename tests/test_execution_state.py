from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pytest
import yaml

from quant_paper_sim.cli import main
from quant_paper_sim.engine import initialize, rebalance, run_step, status
from quant_paper_sim.execution import compile_log, lot_size, normalize_symbol
from quant_paper_sim.models import PortfolioState, SignalBundle, TargetPosition
from quant_paper_sim.readers.signals import load_config, load_signals
from quant_paper_sim.state import StateError, load_log

ROOT = Path(__file__).resolve().parents[1]


def write_signal(
    path: Path,
    *,
    as_of: str,
    targets: list[tuple[str, float, float]],
    cash_reserve: float = 0.01,
) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "as_of": as_of,
                "cash_reserve": cash_reserve,
                "targets": [
                    {"symbol": symbol, "weight": weight, "price": price}
                    for symbol, weight, price in targets
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def paper_config(
    tmp_path: Path,
    *,
    initial_capital: int = 10_000,
    commission_rate: str = "0.0003",
    stamp_duty_rate: str = "0.0005",
) -> tuple[Path, Path, Path]:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    signal_path = tmp_path / "signals.yaml"
    config_path = config_dir / "paper.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(state_dir),
                "initial_capital": initial_capital,
                "cash_reserve": 0.01,
                "commission_rate": float(commission_rate),
                "stamp_duty_rate": float(stamp_duty_rate),
                "stale_exit_price_policy": "last_known_signal_close",
                "signals": {"source": "yaml", "path": str(signal_path)},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path, signal_path, state_dir


def test_cli_init_step_status_and_authoritative_files(tmp_path: Path, capsys) -> None:
    config, signal, state_dir = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])

    main(["init", "--config", str(config)])
    assert "authoritative paper ledger" in capsys.readouterr().out
    main(["step", "--config", str(config)])
    assert "fills=1" in capsys.readouterr().out
    main(["status", "--config", str(config)])
    printed = json.loads(capsys.readouterr().out)

    assert printed["authoritative"] is True
    assert printed["cash"] < 10_000
    assert (state_dir / "execution_log.json").is_file()
    assert (state_dir / "execution_artifacts.json").is_file()
    assert (state_dir / "projection_manifest.json").is_file()
    assert not (state_dir / ".paper_commit_pending.json").exists()


def test_projection_tampering_never_changes_authoritative_state(tmp_path: Path) -> None:
    config, signal, state_dir = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    expected = run_step(config).portfolio
    tampered = expected.to_dict()
    tampered["cash"] = 9_999_999
    tampered["holdings"] = []
    (state_dir / "portfolio.json").write_text(json.dumps(tampered), encoding="utf-8")

    replayed = status(config)
    repaired = json.loads((state_dir / "portfolio.json").read_text(encoding="utf-8"))

    assert replayed.cash == expected.cash
    assert replayed.holdings == expected.holdings
    assert repaired["cash"] == expected.cash
    assert repaired["holdings"]


def test_low_cash_rotation_sells_then_buys_from_actual_fill_proceeds(tmp_path: Path) -> None:
    config, signal, state_dir = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 1.0)])
    first = run_step(config)
    assert first.portfolio.cash < 100
    assert first.portfolio.holdings[0].shares == 9900

    write_signal(signal, as_of="2026-07-29", targets=[("000002", 1.0, 1.0)])
    second = run_step(config)
    assert [(trade["side"], trade["symbol"]) for trade in second.trades] == [
        ("sell", "000001"),
        ("buy", "000002"),
    ]
    assert (
        second.portfolio.holdings == [second.portfolio.holdings[0]]
        and second.portfolio.holdings[0].symbol == "000002"
    )
    assert second.portfolio.holdings[0].shares == 9800
    assert second.trades[0]["event_time"] < second.trades[1]["event_time"]

    log = json.loads((state_dir / "execution_log.json").read_text(encoding="utf-8"))
    phases = log["steps"][-1]["replay_evidence"]["event_ids"]
    assert any(":submit-sell" in value for value in phases)
    assert any(":fill-sell:" in value for value in phases)
    assert any(":submit-buy" in value for value in phases)
    assert any(":fill-buy:" in value for value in phases)
    assert phases.index(next(value for value in phases if ":fill-sell:" in value)) < phases.index(
        next(value for value in phases if ":submit-buy" in value)
    )

    artifacts = json.loads((state_dir / "execution_artifacts.json").read_text(encoding="utf-8"))
    latest_fill_ids = [trade["fill_id"] for trade in second.trades]
    assert latest_fill_ids == log["steps"][-1]["replay_evidence"]["fill_ids"]
    ledger_fill_refs = [
        row["reference_id"] for row in artifacts["ledger"] if row["event_type"] == "fill"
    ]
    assert ledger_fill_refs.index(latest_fill_ids[0]) < ledger_fill_refs.index(latest_fill_ids[1])
    sell_event = next(row for row in artifacts["market_events"] if ":fill-sell:" in row["event_id"])
    assert "carried_forward_prior_signal_close" in sell_event["source"]


def test_same_trading_day_rotation_is_rejected_by_t_plus_one(tmp_path: Path) -> None:
    config, signal, state_dir = paper_config(tmp_path)
    write_signal(
        signal,
        as_of="2026-07-28T07:00:00+00:00",
        targets=[("000001", 1.0, 10.0)],
    )
    run_step(config)
    write_signal(
        signal,
        as_of="2026-07-28T08:00:00+00:00",
        targets=[("000002", 1.0, 10.0)],
    )
    rejected = run_step(config)

    assert rejected.trades == []
    assert rejected.portfolio.holdings[0].symbol == "000001"
    artifacts = json.loads((state_dir / "execution_artifacts.json").read_text(encoding="utf-8"))
    reasons = [row["reason"] for row in artifacts["order_events"]]
    assert any("A_SHARE_T_PLUS_ONE" in reason for reason in reasons)
    assert any("INSUFFICIENT" in reason for reason in reasons)


def test_fees_reduce_cash_and_nav_and_trades_are_fills(tmp_path: Path) -> None:
    config, signal, state_dir = paper_config(tmp_path, initial_capital=100_000)
    write_signal(
        signal,
        as_of="2026-07-28",
        targets=[("000001", 1.0, 10.0)],
        cash_reserve=0.05,
    )
    result = run_step(config)
    assert len(result.trades) == 1
    fee = result.trades[0]["fee"]
    assert fee > 0
    assert result.portfolio.nav == pytest.approx(100_000 - fee)
    artifacts = json.loads((state_dir / "execution_artifacts.json").read_text(encoding="utf-8"))
    assert result.trades[0]["fill_id"] in {row["fill_id"] for row in artifacts["fills"]}
    assert fee == pytest.approx(
        sum(row["amount"]["units"] / 10 ** row["amount"]["scale"] for row in artifacts["fees"])
    )


def test_same_as_of_is_idempotent_but_conflicting_content_fails_closed(tmp_path: Path) -> None:
    config, signal, state_dir = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    first = run_step(config)
    log_before = (state_dir / "execution_log.json").read_bytes()
    second = run_step(config)
    assert second.to_dict() == first.to_dict()
    assert (state_dir / "execution_log.json").read_bytes() == log_before
    assert len(pd.read_csv(state_dir / "nav.csv")) == 1

    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.01)])
    with pytest.raises(StateError, match="conflicting"):
        run_step(config)
    assert (state_dir / "execution_log.json").read_bytes() == log_before


def test_cross_process_status_preserves_replay_hashes(tmp_path: Path) -> None:
    config, signal, state_dir = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    run_step(config)
    before = load_log(state_dir / "execution_log.json")["steps"][-1]["cumulative_result"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    command = [sys.executable, "-m", "quant_paper_sim.cli", "status", "--config", str(config)]
    for _ in range(2):
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(completed.stdout)["authoritative"] is True
    after = load_log(state_dir / "execution_log.json")["steps"][-1]["cumulative_result"]
    assert after == before
    assert all(
        before[key] == after[key]
        for key in ("order_sha256", "fill_sha256", "ledger_sha256", "result_sha256")
    )


def test_damaged_log_and_legacy_holdings_fail_closed(tmp_path: Path) -> None:
    config, signal, state_dir = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    initialize(config)
    (state_dir / "execution_log.json").write_text('{"schema":', encoding="utf-8")
    with pytest.raises(StateError, match="cannot read authoritative"):
        status(config)

    shutil.rmtree(state_dir)
    state_dir.mkdir()
    (state_dir / "portfolio.json").write_text(
        json.dumps(
            {
                "cash": 1000,
                "holdings": [{"symbol": "000001", "shares": 100, "price": 10}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StateError, match="cannot be migrated losslessly"):
        status(config)


def test_cash_only_legacy_state_is_explicitly_migrated(tmp_path: Path) -> None:
    config, _, state_dir = paper_config(tmp_path)
    state_dir.mkdir()
    (state_dir / "portfolio.json").write_text(
        json.dumps({"as_of": "init", "cash": 4321, "holdings": []}), encoding="utf-8"
    )
    migrated = status(config)
    log = load_log(state_dir / "execution_log.json")
    assert migrated.cash == 4321
    assert log["migration"]["kind"] == "legacy_cash_only"


def test_fixed_point_price_board_lot_mapping_and_no_live_path() -> None:
    portfolio = PortfolioState(as_of="", cash=100_000, initial_capital=100_000)
    signals = SignalBundle(
        as_of="2026-07-28",
        targets=[TargetPosition("688001.SH", 1.0, 10.01)],
        cash_reserve=0.05,
    )
    result = rebalance(portfolio, signals)
    assert lot_size("688001") == 200
    assert result.portfolio.holdings[0].shares % 200 == 0
    assert normalize_symbol("688001.SH") == ("688001", "XSHG", "CN.XSHG.688001")
    with pytest.raises(StateError, match="not exact at scale 2"):
        rebalance(
            portfolio,
            SignalBundle(
                as_of="2026-07-28",
                targets=[TargetPosition("688001", 1.0, 10.001)],
            ),
        )


def test_pipeline_command_and_relative_paths_remain_supported(tmp_path: Path) -> None:
    (tmp_path / "configs").mkdir()
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    shutil.copy(ROOT / "configs" / "pipeline.yaml", tmp_path / "configs" / "pipeline.yaml")
    shutil.copy(
        ROOT / "tests" / "fixtures" / "signals.yaml",
        tmp_path / "tests" / "fixtures" / "signals.yaml",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "quant_paper_sim.cli", "step", "--config", "configs/pipeline.yaml"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "paper step" in completed.stdout
    assert (tmp_path / "state" / "execution_log.json").is_file()


def test_factor_csv_is_a_real_supported_source_and_unknown_source_fails(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    config = config_dir / "factor.yaml"
    fixture = ROOT / "tests" / "fixtures" / "factor_signals.csv"
    config.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(state_dir),
                "initial_capital": 100_000,
                "signals": {
                    "source": "csv",
                    "path": str(fixture),
                    "factor_column": "momentum_20d",
                    "price_column": "close",
                    "top_n": 2,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    bundle = load_signals(load_config(config), config)
    assert [target.symbol for target in bundle.targets] == ["000001", "000002"]
    assert [target.price for target in bundle.targets] == [12.0, 8.5]
    result = run_step(config)
    assert result.portfolio.holdings

    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["signals"]["source"] = "mystery"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported signals.source"):
        load_signals(load_config(config), config)


def test_replayed_engine_and_broker_have_no_live_order_path(tmp_path: Path) -> None:
    config, signal, state_dir = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    run_step(config)
    compiled = compile_log(load_log(state_dir / "execution_log.json"))
    assert compiled.runtime.engine.sends_live_orders is False
    assert compiled.runtime.engine.broker.sends_live_orders is False
    artifacts = compiled.runtime.engine.artifacts
    assert artifacts is not None
    event_times = [event.available_at for event in artifacts.market_events]
    sequences = [event.sequence for event in artifacts.market_events]
    assert all(left < right for left, right in pairwise(event_times))
    assert sequences == sorted(sequences)
    assert all(event.event_time.tzinfo is not None for event in artifacts.market_events)
    assert all("not_live" in event.source for event in artifacts.market_events)
