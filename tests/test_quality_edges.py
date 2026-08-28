from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml
from quant_data_kit import AssetClass

import quant_paper_sim.engine as paper_engine
import quant_paper_sim.state as paper_state
from quant_paper_sim.engine import (
    append_nav,
    load_portfolio,
    rebalance,
    run_step,
    save_portfolio,
    status,
    write_holdings_csv,
)
from quant_paper_sim.execution import (
    IntentStrategy,
    _desired_quantities,
    artifact_payloads,
    decimal_value,
    instrument_spec,
    parse_as_of,
)
from quant_paper_sim.models import Holding, PortfolioState, SignalBundle, TargetPosition
from quant_paper_sim.readers.signals import (
    load_factor_csv,
    load_q5_csv,
    load_signals,
    resolve_data_path,
)
from quant_paper_sim.state import StateError, StatePaths, load_log, new_log, seal_log, validate_log


def make_case(tmp_path: Path, *, state_dir: str | None = None) -> tuple[Path, Path, Path]:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    signal = tmp_path / "signal.yaml"
    state = tmp_path / "state"
    config = config_dir / "run.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "state_dir": state_dir if state_dir is not None else str(state),
                "initial_capital": 10_000,
                "cash_reserve": 0.05,
                "commission_rate": 0.0003,
                "stamp_duty_rate": 0.0005,
                "signals": {"source": "yaml", "path": str(signal)},
            }
        ),
        encoding="utf-8",
    )
    return config, signal, state


def write_signal(
    path: Path,
    *,
    as_of: str = "2026-07-28",
    targets: list[dict] | None = None,
) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "as_of": as_of,
                "targets": targets
                if targets is not None
                else [{"symbol": "000001", "weight": 1.0, "price": 10.0}],
            }
        ),
        encoding="utf-8",
    )


def reseal_to(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(seal_log(payload), indent=2), encoding="utf-8")


def test_relative_state_path_invalid_policy_and_config_drift(tmp_path: Path) -> None:
    config, signal, _ = make_case(tmp_path, state_dir="state")
    write_signal(signal)
    result = run_step(config)
    relative_state = tmp_path / "state"
    assert result.portfolio.authoritative
    assert (relative_state / "execution_log.json").is_file()

    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["commission_rate"] = 0.001
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(StateError, match="configuration differs"):
        status(config)
    raw["stale_exit_price_policy"] = "invent_a_price"
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(StateError, match="stale_exit_price_policy"):
        status(config)


def test_missing_signal_path_source_file_and_invalid_signal_inputs(tmp_path: Path) -> None:
    config, signal, _ = make_case(tmp_path)
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    del raw["signals"]["path"]
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(StateError, match="signals.path"):
        run_step(config)

    raw["signals"]["path"] = str(tmp_path / "missing.yaml")
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(StateError, match="does not exist"):
        run_step(config)

    raw["signals"]["path"] = str(signal)
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    write_signal(signal, targets=[])
    with pytest.raises(StateError, match="targets are empty"):
        run_step(config)
    write_signal(
        signal,
        targets=[
            {"symbol": "000001", "weight": 0.5, "price": 10},
            {"symbol": "000001.SZ", "weight": 0.5, "price": 10},
        ],
    )
    with pytest.raises(StateError, match="duplicate target"):
        run_step(config)
    write_signal(signal, targets=[{"symbol": "000001", "weight": -1, "price": 10}])
    with pytest.raises(StateError, match="non-negative"):
        run_step(config)


def test_old_step_pending_marker_and_evidence_tampering_fail_closed(tmp_path: Path) -> None:
    config, signal, state = make_case(tmp_path)
    write_signal(signal, as_of="2026-07-29")
    run_step(config)
    write_signal(signal, as_of="2026-07-28")
    with pytest.raises(StateError, match="strictly increasing"):
        run_step(config)

    pending = state / ".paper_commit_pending.json"
    pending.write_text("not-json", encoding="utf-8")
    with pytest.raises(StateError, match="damaged pending"):
        status(config)
    pending.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
    with pytest.raises(StateError, match="damaged pending"):
        status(config)
    pending.unlink()

    log_path = state / "execution_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8"))
    log["steps"][-1]["replay_evidence"]["fill_ids"] = ["fabricated-fill"]
    reseal_to(log_path, log)
    with pytest.raises(StateError, match="replayed fills differ"):
        status(config)


def test_missing_log_with_interrupted_v2_commit_never_migrates_projection_cash(
    tmp_path: Path, monkeypatch
) -> None:
    config, signal, state = make_case(tmp_path)
    write_signal(signal)
    real_atomic_write_json = paper_engine.atomic_write_json

    def fail_before_authoritative_log(path: Path, payload: object) -> None:
        if path.name == "execution_log.json":
            raise OSError("injected crash before authoritative log")
        real_atomic_write_json(path, payload)

    monkeypatch.setattr(paper_engine, "atomic_write_json", fail_before_authoritative_log)
    with pytest.raises(OSError, match="injected crash"):
        run_step(config)
    monkeypatch.setattr(paper_engine, "atomic_write_json", real_atomic_write_json)

    assert not (state / "execution_log.json").exists()
    assert (state / ".paper_commit_pending.json").is_file()
    projection = json.loads((state / "portfolio.json").read_text(encoding="utf-8"))
    projection["cash"] = 777
    (state / "portfolio.json").write_text(json.dumps(projection), encoding="utf-8")

    with pytest.raises(StateError, match="v2 state residue"):
        status(config)
    assert not (state / "execution_log.json").exists()


def test_deleted_log_with_v2_projection_markers_fails_closed(tmp_path: Path) -> None:
    config, signal, state = make_case(tmp_path)
    write_signal(signal)
    run_step(config)
    (state / "execution_log.json").unlink()
    for name in (
        ".paper_commit_pending.json",
        "projection_manifest.json",
        "execution_artifacts.json",
    ):
        (state / name).unlink(missing_ok=True)
    projection = json.loads((state / "portfolio.json").read_text(encoding="utf-8"))
    projection["cash"] = 777
    (state / "portfolio.json").write_text(json.dumps(projection), encoding="utf-8")

    with pytest.raises(StateError, match="portfolio.json markers"):
        status(config)
    assert not (state / "execution_log.json").exists()


@pytest.mark.parametrize("residue_name", ["projection_manifest.json", "execution_artifacts.json"])
def test_missing_log_with_v2_projection_file_fails_closed(
    tmp_path: Path, residue_name: str
) -> None:
    config, _, state = make_case(tmp_path)
    state.mkdir()
    (state / residue_name).write_text("{}", encoding="utf-8")
    with pytest.raises(StateError, match="v2 state residue"):
        status(config)
    assert not (state / "execution_log.json").exists()


def test_valid_pending_with_old_log_replays_and_repairs_projections(tmp_path: Path) -> None:
    config, signal, state = make_case(tmp_path)
    write_signal(signal)
    expected = run_step(config).portfolio
    projection = json.loads((state / "portfolio.json").read_text(encoding="utf-8"))
    projection["cash"] = 777
    (state / "portfolio.json").write_text(json.dumps(projection), encoding="utf-8")
    (state / ".paper_commit_pending.json").write_text(
        json.dumps(
            {
                "schema": "quant-paper-commit/1.0.0",
                "target_authoritative_sha256": "0" * 64,
                "files": [
                    "execution_artifacts.json",
                    "holdings.csv",
                    "nav.csv",
                    "portfolio.json",
                    "trades.json",
                ],
            }
        ),
        encoding="utf-8",
    )

    replayed = status(config)
    repaired = json.loads((state / "portfolio.json").read_text(encoding="utf-8"))
    assert replayed.cash == expected.cash
    assert repaired["cash"] == expected.cash
    assert not (state / ".paper_commit_pending.json").exists()


def test_cumulative_hash_tampering_and_malformed_legacy_json_fail_closed(tmp_path: Path) -> None:
    config, signal, state = make_case(tmp_path)
    write_signal(signal)
    run_step(config)
    log_path = state / "execution_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8"))
    log["steps"][-1]["cumulative_result"]["ledger_sha256"] = "0" * 64
    reseal_to(log_path, log)
    with pytest.raises(StateError, match="hashes differ"):
        status(config)

    for child in state.iterdir():
        child.unlink()
    (state / "portfolio.json").write_text("{", encoding="utf-8")
    with pytest.raises(StateError, match="legacy portfolio cannot be read"):
        status(config)


def test_status_requires_existing_state_and_rebalance_refuses_legacy_holdings(
    tmp_path: Path,
) -> None:
    config, _, _ = make_case(tmp_path)
    with pytest.raises(StateError, match="run quant-paper init"):
        status(config)
    with pytest.raises(StateError, match="cannot be migrated losslessly"):
        rebalance(
            PortfolioState(
                as_of="2026-07-28",
                cash=1000,
                holdings=[Holding("000001", 100, 10)],
                initial_capital=2000,
            ),
            SignalBundle(
                as_of="2026-07-29",
                targets=[TargetPosition("000002", 1, 10)],
            ),
        )


def test_zero_cash_never_reopens_initial_capital(tmp_path: Path) -> None:
    signals = SignalBundle(
        as_of="2026-07-29",
        targets=[TargetPosition("000002", 1, 10)],
    )
    with pytest.raises(StateError, match="initial_capital cannot recreate"):
        rebalance(
            PortfolioState(as_of="2026-07-28", cash=0, initial_capital=100_000),
            signals,
        )

    config, _, state = make_case(tmp_path)
    state.mkdir()
    (state / "portfolio.json").write_text(
        json.dumps({"as_of": "init", "cash": 0, "holdings": []}), encoding="utf-8"
    )
    with pytest.raises(StateError, match="initial_cash must be a positive"):
        status(config)
    assert not (state / "execution_log.json").exists()


def test_legacy_projection_helpers_remain_compatible(tmp_path: Path) -> None:
    missing = load_portfolio(tmp_path / "missing.json", 1234)
    assert missing.cash == 1234
    portfolio = PortfolioState(
        as_of="2026-07-28",
        cash=1000,
        holdings=[Holding("000001", 100, 10)],
        initial_capital=2000,
    )
    portfolio_path = tmp_path / "portfolio.json"
    save_portfolio(portfolio_path, portfolio)
    assert load_portfolio(portfolio_path, 0).to_dict() == portfolio.to_dict()
    nav_path = tmp_path / "nav.csv"
    append_nav(nav_path, portfolio)
    append_nav(nav_path, portfolio)
    assert len(pd.read_csv(nav_path)) == 2
    holdings_path = tmp_path / "holdings.csv"
    write_holdings_csv(holdings_path, portfolio)
    assert pd.read_csv(holdings_path).iloc[0]["shares"] == 100
    empty = PortfolioState(as_of="init", cash=0, holdings=[])
    write_holdings_csv(holdings_path, empty)
    assert pd.read_csv(holdings_path).empty


@pytest.mark.parametrize("value", [None, object(), "NaN", "Infinity"])
def test_decimal_and_time_inputs_fail_closed(value: object) -> None:
    with pytest.raises(StateError, match="finite decimal"):
        decimal_value(value, "value")
    with pytest.raises(StateError, match="as_of"):
        parse_as_of("")
    with pytest.raises(StateError, match="ISO date"):
        parse_as_of("bad-date")
    with pytest.raises(StateError, match="timezone-aware UTC"):
        parse_as_of("2026-07-28T09:00:00")
    with pytest.raises(StateError, match="timezone-aware UTC"):
        parse_as_of("2026-07-28T09:00:00+08:00")


@pytest.mark.parametrize(
    "symbol",
    ["", "ABC", "000001.SZ.EXTRA", "000001.NY", "830001", "000001.SH", "600000.SZ"],
)
def test_invalid_symbols_fail_closed(symbol: str) -> None:
    with pytest.raises(StateError):
        normalize = paper_engine.normalize_symbol
        normalize(symbol)


@pytest.mark.parametrize(
    "symbol",
    [
        "920000",
        "900901.SH",
        "200002.SZ",
        "399001.SZ",
        "123001.SZ",
        "131810.SZ",
        "160706.SZ",
        "184801.SZ",
        "508000.SH",
        "580000.SH",
    ],
)
def test_out_of_scope_exchange_products_fail_closed(symbol: str) -> None:
    with pytest.raises(StateError, match="outside certified profile"):
        paper_engine.normalize_symbol(symbol)


def test_negative_or_unreasonable_fee_rates_fail_before_state_changes(tmp_path: Path) -> None:
    config, signal, state = make_case(tmp_path)
    write_signal(signal)
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw["commission_rate"] = -0.001
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(StateError, match="commission_rate must be in"):
        run_step(config)
    assert not (state / "execution_log.json").exists()

    raw["commission_rate"] = 0.0003
    raw["stamp_duty_rate"] = 0.0005
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    first = run_step(config)
    log_before = (state / "execution_log.json").read_bytes()
    artifacts_before = (state / "execution_artifacts.json").read_bytes()
    write_signal(
        signal,
        as_of="2026-07-29",
        targets=[{"symbol": "000002", "weight": 1, "price": 10}],
    )
    raw["stamp_duty_rate"] = -0.001
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(StateError, match="stamp_duty_rate must be in"):
        run_step(config)
    assert (state / "execution_log.json").read_bytes() == log_before
    assert (state / "execution_artifacts.json").read_bytes() == artifacts_before

    raw["stamp_duty_rate"] = 0.0005
    config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert status(config).nav == first.portfolio.nav
    with pytest.raises(StateError, match="commission_rate must be in"):
        rebalance(
            PortfolioState(as_of="", cash=1000),
            SignalBundle(
                as_of="2026-07-29",
                targets=[TargetPosition("000001", 1, 10)],
            ),
            commission_rate=0.1001,
        )


@pytest.mark.parametrize(
    ("commission", "stamp", "field"),
    [("NaN", "0.0005", "commission_rate"), ("0.0003", "Infinity", "stamp_duty_rate")],
)
def test_non_finite_fee_rates_fail_closed(commission: str, stamp: str, field: str) -> None:
    with pytest.raises(StateError, match=f"{field} must be a finite decimal"):
        instrument_spec("000001", commission_rate=commission, stamp_duty_rate=stamp)


def test_desired_quantity_and_instrument_edge_rules() -> None:
    base = {
        "cash_reserve": "0.05",
        "regime_scale": "1",
        "targets": [
            {
                "weight": "1",
                "price": "10",
                "lot_size": 100,
                "instrument_id": "CN.XSHE.000001",
            }
        ],
    }
    assert _desired_quantities(base, Decimal(100000))["CN.XSHE.000001"] == 9500
    for field, value, message in (
        ("cash_reserve", "1", "cash_reserve"),
        ("regime_scale", "2", "regime_scale"),
    ):
        broken = deepcopy(base)
        broken[field] = value
        with pytest.raises(StateError, match=message):
            _desired_quantities(broken, Decimal(100000))
    zero = deepcopy(base)
    zero["targets"][0]["weight"] = "0"
    with pytest.raises(StateError, match="sum to a positive"):
        _desired_quantities(zero, Decimal(100000))
    negative = deepcopy(base)
    negative["targets"].append(
        {"weight": "-0.1", "price": "10", "lot_size": 100, "instrument_id": "x"}
    )
    with pytest.raises(StateError, match="non-negative"):
        _desired_quantities(negative, Decimal(100000))
    bad_price = deepcopy(base)
    bad_price["targets"][0]["price"] = "0"
    with pytest.raises(StateError, match="price must be positive"):
        _desired_quantities(bad_price, Decimal(100000))

    etf = instrument_spec("510300.SH", commission_rate="0.0003", stamp_duty_rate="0.0005")
    equity = instrument_spec("600519", commission_rate="0.0003", stamp_duty_rate="0.0005")
    assert etf.asset_class is AssetClass.ETF
    assert etf.metadata["stamp_duty_rate"] == "0"
    assert etf.price_tick.scale == 3
    assert etf.price_tick.units == 1
    assert equity.asset_class is AssetClass.EQUITY
    assert equity.metadata["stamp_duty_rate"] == "0.0005"
    assert equity.price_tick.scale == 2
    assert equity.price_tick.units == 1


def test_core_etfs_trade_at_three_decimals_and_replay_exactly(tmp_path: Path) -> None:
    config, signal, state = make_case(tmp_path)
    write_signal(
        signal,
        targets=[
            {"symbol": "510300.SH", "weight": 0.5, "price": 3.927},
            {"symbol": "159919.SZ", "weight": 0.5, "price": 4.123},
        ],
    )
    first = run_step(config)
    log_before = load_log(state / "execution_log.json")
    artifacts = json.loads((state / "execution_artifacts.json").read_text(encoding="utf-8"))

    assert {trade["symbol"] for trade in first.trades} == {"510300.SH", "159919.SZ"}
    assert {row["price"]["scale"] for row in artifacts["fills"]} == {3}
    assert {row["price"]["units"] for row in artifacts["fills"]} == {3927, 4123}
    assert {row["price_scale"] for row in log_before["steps"][0]["targets"]} == {3}
    assert status(config).to_dict() == first.portfolio.to_dict()
    log_after = load_log(state / "execution_log.json")
    assert log_after["content_sha256"] == log_before["content_sha256"]
    assert (
        log_after["steps"][-1]["cumulative_result"] == log_before["steps"][-1]["cumulative_result"]
    )


def test_compile_log_rejects_mapping_day_and_event_time_tampering(tmp_path: Path) -> None:
    config, signal, state = make_case(tmp_path)
    write_signal(signal)
    run_step(config)
    original = json.loads((state / "execution_log.json").read_text(encoding="utf-8"))

    mapping = deepcopy(original)
    mapping["steps"][0]["targets"][0]["instrument_id"] = "CN.XSHG.600000"
    mapping["steps"][0]["input_sha256"] = paper_state.sha256_payload(
        paper_state.step_input_payload(mapping["steps"][0])
    )
    with pytest.raises(StateError, match="mapping"):
        paper_engine._compile(seal_log(mapping))

    day = deepcopy(original)
    day["steps"][0]["trading_day"] = "2026-07-29"
    day["steps"][0]["input_sha256"] = paper_state.sha256_payload(
        paper_state.step_input_payload(day["steps"][0])
    )
    with pytest.raises(StateError, match="trading_day"):
        paper_engine._compile(seal_log(day))

    close_time = deepcopy(original)
    second = deepcopy(close_time["steps"][0])
    second["as_of"] = "2026-07-28T07:00:00.000001+00:00"
    second["input_sha256"] = paper_state.sha256_payload(paper_state.step_input_payload(second))
    close_time["steps"].append(second)
    with pytest.raises(StateError, match="event time"):
        paper_engine._compile(seal_log(close_time))


def test_strategy_empty_artifacts_and_replay_count_defenses(tmp_path: Path) -> None:
    strategy = IntentStrategy({})
    strategy.reset()
    assert strategy.on_event(None, SimpleNamespace(event_id="none")) == ()
    fake = SimpleNamespace(runtime=SimpleNamespace(engine=SimpleNamespace(artifacts=None)))
    assert artifact_payloads(fake) == {
        "market_events": [],
        "orders": [],
        "order_events": [],
        "fills": [],
        "fees": [],
        "ledger": [],
    }
    config, signal, state = make_case(tmp_path)
    write_signal(signal)
    run_step(config)
    log = json.loads((state / "execution_log.json").read_text(encoding="utf-8"))
    with pytest.raises(StateError, match="step count"):
        paper_engine._validate_replay_evidence(log, SimpleNamespace(steps=()))


def test_state_schema_validation_and_atomic_cleanup(tmp_path: Path, monkeypatch) -> None:
    config = {
        "commission_rate": "0.0003",
        "stamp_duty_rate": "0.0005",
    }
    valid = new_log(initial_cash="100", execution_config=config)
    with pytest.raises(StateError, match="JSON object"):
        validate_log([])
    for mutation, message in (
        (lambda value: value.pop("schema"), "missing fields"),
        (lambda value: value.__setitem__("schema", "wrong"), "unsupported"),
        (lambda value: value.__setitem__("quant_execution_version", "9"), "requires"),
        (lambda value: value.__setitem__("account_id", "other"), "identity"),
        (lambda value: value.__setitem__("execution_config", []), "execution_config"),
        (lambda value: value.__setitem__("steps", {}), "steps must"),
    ):
        broken = deepcopy(valid)
        mutation(broken)
        broken = seal_log(broken)
        with pytest.raises(StateError, match=message):
            validate_log(broken)
    checksum = deepcopy(valid)
    checksum["initial_cash"] = "101"
    with pytest.raises(StateError, match="checksum"):
        validate_log(checksum)
    with pytest.raises(StateError, match="positive"):
        new_log(initial_cash=0, execution_config=config)

    target = tmp_path / "atomic.json"
    original_replace = paper_state.os.replace

    def fail_replace(source, destination):
        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr(paper_state.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        paper_state.atomic_write_json(target, {"safe": True})
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(paper_state.os, "replace", original_replace)


def test_state_path_properties_and_reader_failures(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)
    assert paths.nav.name == "nav.csv"
    assert paths.holdings.name == "holdings.csv"
    assert paths.trades.name == "trades.json"

    empty_q5 = tmp_path / "q5_candidates_2026-01-01.csv"
    empty_q5.write_text("symbol,close\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty q5"):
        load_q5_csv(empty_q5, 3, 0.05, 1)

    factor = tmp_path / "factor.csv"
    factor.write_text("symbol,date,score\n000001,2026-01-01,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        load_factor_csv(
            factor,
            factor_column="score",
            price_column="close",
            top_n=1,
            cash_reserve=0.05,
            regime_scale=1,
        )
    factor.write_text("symbol,date,score,close\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty factor"):
        load_factor_csv(
            factor,
            factor_column="score",
            price_column="close",
            top_n=1,
            cash_reserve=0.05,
            regime_scale=1,
        )
    factor.write_text("symbol,date,score,close\n000001,2026-01-01,,10\n", encoding="utf-8")
    with pytest.raises(ValueError, match="finite numeric"):
        load_factor_csv(
            factor,
            factor_column="score",
            price_column="close",
            top_n=1,
            cash_reserve=0.05,
            regime_scale=1,
        )


@pytest.mark.parametrize("top_n", [0, -1])
def test_csv_top_n_must_be_positive_for_q5_and_factor(tmp_path: Path, top_n: int) -> None:
    q5 = tmp_path / "q5_candidates_2026-01-01.csv"
    q5.write_text("symbol,close\n000001,10\n", encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        load_q5_csv(q5, top_n, 0.05, 1)

    factor = tmp_path / "factor.csv"
    factor.write_text("symbol,date,score,close\n000001,2026-01-01,1,10\n", encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        load_factor_csv(
            factor,
            factor_column="score",
            price_column="close",
            top_n=top_n,
            cash_reserve=0.05,
            regime_scale=1,
        )


@pytest.mark.parametrize(
    ("weights", "message"),
    [
        ("-1,1", "non-negative"),
        ("0,0", "total must be positive"),
        ("NaN,1", "finite numeric"),
        ("Inf,1", "finite numeric"),
    ],
)
def test_explicit_csv_weights_never_fall_back_to_equal_weight(
    tmp_path: Path, weights: str, message: str
) -> None:
    first, second = weights.split(",")
    q5 = tmp_path / "q5_candidates_2026-01-01.csv"
    q5.write_text(
        f"symbol,close,weight\n000001,10,{first}\n000002,20,{second}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        load_q5_csv(q5, 2, 0.05, 1)

    factor = tmp_path / "factor.csv"
    factor.write_text(
        "symbol,date,score,close,weight\n"
        f"000001,2026-01-01,2,10,{first}\n"
        f"000002,2026-01-01,1,20,{second}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        load_factor_csv(
            factor,
            factor_column="score",
            price_column="close",
            top_n=2,
            cash_reserve=0.05,
            regime_scale=1,
        )


@pytest.mark.parametrize(
    ("factor", "price", "message"),
    [
        ("NaN", "10", "finite numeric"),
        ("Inf", "10", "finite numeric"),
        ("1", "NaN", "finite numeric"),
        ("1", "Inf", "finite numeric"),
        ("1", "0", "positive"),
    ],
)
def test_factor_and_price_columns_must_be_finite_and_price_positive(
    tmp_path: Path, factor: str, price: str, message: str
) -> None:
    csv_path = tmp_path / "factor.csv"
    csv_path.write_text(
        f"symbol,date,score,close\n000001,2026-01-01,{factor},{price}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        load_factor_csv(
            csv_path,
            factor_column="score",
            price_column="close",
            top_n=1,
            cash_reserve=0.05,
            regime_scale=1,
        )

    q5 = tmp_path / "q5_candidates_2026-01-01.csv"
    q5.write_text(f"symbol,close\n000001,{price}\n", encoding="utf-8")
    if price in {"NaN", "Inf", "0"}:
        with pytest.raises(ValueError, match=message):
            load_q5_csv(q5, 1, 0.05, 1)


def test_reader_equal_weights_override_and_path_resolution(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config = config_dir / "run.yaml"
    factor = tmp_path / "factor.csv"
    factor.write_text(
        "symbol,date,score,close\n000001,2026-01-01,2,10\n000002,2026-01-01,1,20\n",
        encoding="utf-8",
    )
    bundle = load_factor_csv(
        factor,
        factor_column="score",
        price_column="close",
        top_n=2,
        cash_reserve=0.05,
        regime_scale=1,
    )
    assert [target.weight for target in bundle.targets] == [0.5, 0.5]
    assert resolve_data_path("factor.csv", config) == factor.resolve()
    assert resolve_data_path(str(factor), config) == factor

    config.write_text(
        yaml.safe_dump(
            {
                "signals": {"source": "csv", "path": str(factor)},
                "regime": {"override_scale": 0.7},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="factor_column"):
        load_signals(yaml.safe_load(config.read_text(encoding="utf-8")), config)
