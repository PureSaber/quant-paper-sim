"""Public paper-sim API backed exclusively by quant-execution replay."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_execution import execution_payload

from quant_paper_sim.execution import (
    MONEY_SCALE,
    CompiledReplay,
    artifact_payloads,
    compile_log,
    decimal_value,
    fixed,
    lot_size,
    normalize_symbol,
    parse_as_of,
)
from quant_paper_sim.models import Holding, PortfolioState, RebalanceResult, SignalBundle
from quant_paper_sim.state import (
    PENDING_FILE,
    StateError,
    StatePaths,
    atomic_write_bytes,
    atomic_write_json,
    file_sha256,
    load_log,
    new_log,
    seal_log,
    sha256_payload,
    step_input_payload,
    validate_log,
)


def _state_paths(config_path: Path, cfg: dict[str, Any]) -> StatePaths:
    repo_root = config_path.resolve().parent.parent
    state_dir = Path(str(cfg.get("state_dir", "state")))
    if not state_dir.is_absolute():
        state_dir = repo_root / state_dir
    return StatePaths(state_dir.resolve())


def _decimal_config(cfg: dict[str, Any], key: str, default: str) -> str:
    return format(decimal_value(cfg.get(key, default), key), "f")


def _execution_config(cfg: dict[str, Any]) -> dict[str, object]:
    policy = str(cfg.get("stale_exit_price_policy", "last_known_signal_close"))
    if policy != "last_known_signal_close":
        raise StateError("stale_exit_price_policy must be 'last_known_signal_close'")
    return {
        "commission_rate": _decimal_config(cfg, "commission_rate", "0.0003"),
        "stamp_duty_rate": _decimal_config(cfg, "stamp_duty_rate", "0.0005"),
        "price_scale": 2,
        "money_scale": MONEY_SCALE,
        "bar_participation_rate": "1",
        "slippage_ticks": 0,
        "seed": 42,
        "market_data_mode": "paper_research_close_not_live",
        "stale_exit_price_policy": policy,
    }


def _assert_config_compatible(log: dict[str, Any], cfg: dict[str, Any]) -> None:
    if log["execution_config"] != _execution_config(cfg):
        raise StateError(
            "execution configuration differs from the authoritative log; "
            "run quant-paper init to start a new state"
        )


def _source_provenance(cfg: dict[str, Any], config_path: Path) -> dict[str, str]:
    from quant_paper_sim.readers.signals import resolve_data_path

    signal_cfg = cfg.get("signals") or {}
    if "path" not in signal_cfg:
        raise StateError("signals.path is required")
    source_path = resolve_data_path(str(signal_cfg["path"]), config_path)
    if not source_path.is_file():
        raise StateError(f"signal source does not exist: {source_path}")
    return {
        "kind": str(signal_cfg.get("source", "yaml")),
        "path": str(source_path.resolve()),
        "sha256": file_sha256(source_path),
        "market_data_semantics": "paper_research_close_not_live",
    }


def _step_record(signals: SignalBundle, *, source: dict[str, str]) -> dict[str, Any]:
    at, trading_day = parse_as_of(signals.as_of)
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in signals.targets:
        code, venue, instrument_id = normalize_symbol(target.symbol)
        if instrument_id in seen:
            raise StateError(f"duplicate target instrument: {instrument_id}")
        seen.add(instrument_id)
        price_fixed = fixed(target.price, 2, f"target[{target.symbol}].price")
        price = price_fixed.to_decimal()
        weight = decimal_value(target.weight, f"target[{target.symbol}].weight")
        if weight < 0:
            raise StateError("target weights must be non-negative")
        targets.append(
            {
                "symbol": str(target.symbol).strip(),
                "native_symbol": f"{code}.{venue}",
                "instrument_id": instrument_id,
                "weight": format(weight, "f"),
                "price": format(price, "f"),
                "price_fixed": {"units": price_fixed.units, "scale": price_fixed.scale},
                "lot_size": lot_size(target.symbol),
            }
        )
    if not targets:
        raise StateError("signal targets are empty")
    step: dict[str, Any] = {
        "as_of": at.isoformat(),
        "trading_day": trading_day.isoformat(),
        "signal_source": source,
        "targets": sorted(targets, key=lambda item: item["instrument_id"]),
        "regime_scale": format(decimal_value(signals.regime_scale, "regime_scale"), "f"),
        "cash_reserve": format(decimal_value(signals.cash_reserve, "cash_reserve"), "f"),
    }
    step["input_sha256"] = sha256_payload(step_input_payload(step))
    return step


def _validate_replay_evidence(log: dict[str, Any], compiled: CompiledReplay) -> None:
    if len(log["steps"]) != len(compiled.steps):
        raise StateError("compiled step count differs from authoritative log")
    for persisted, view in zip(log["steps"], compiled.steps, strict=True):
        evidence = persisted.get("replay_evidence")
        if evidence is not None and evidence.get("fill_ids") != list(view.fill_ids):
            raise StateError(f"replayed fills differ at {persisted['as_of']}")
    if log["steps"]:
        expected = log["steps"][-1].get("cumulative_result")
        artifacts = compiled.runtime.engine.artifacts
        if artifacts is None:
            raise StateError("quant-execution did not produce replay artifacts")
        actual = execution_payload(artifacts.result)
        if expected is not None and expected != actual:
            raise StateError("replayed order/fill/ledger hashes differ from authoritative evidence")


def _compile(log: dict[str, Any]) -> CompiledReplay:
    validated = validate_log(log)
    compiled = compile_log(validated)
    _validate_replay_evidence(validated, compiled)
    if (
        compiled.runtime.engine.sends_live_orders
        or compiled.runtime.engine.broker.sends_live_orders
    ):
        raise StateError("live order path is forbidden")
    return compiled


def _portfolio_from(compiled: CompiledReplay, log: dict[str, Any], as_of: str) -> PortfolioState:
    snapshot = compiled.runtime.snapshot
    cash = snapshot.cash_balances.get("CNY")
    holdings = [
        Holding(
            symbol=compiled.instrument_symbols[instrument_id],
            shares=int(quantity.to_decimal()),
            price=float(compiled.latest_prices[instrument_id]),
        )
        for instrument_id, quantity in sorted(snapshot.positions.items())
        if quantity.units != 0
    ]
    return PortfolioState(
        as_of=as_of,
        cash=float(cash.to_decimal() if cash is not None else Decimal(0)),
        holdings=holdings,
        initial_capital=float(decimal_value(log["initial_cash"], "initial_cash")),
        authoritative=True,
        execution_log_sha256=log["content_sha256"],
    )


def _latest_trades(compiled: CompiledReplay) -> list[dict[str, Any]]:
    if not compiled.steps:
        return []
    fill_ids = set(compiled.steps[-1].fill_ids)
    artifacts = compiled.runtime.engine.artifacts
    if artifacts is None:
        return []
    fees = {fee.fill_id: fee for fee in artifacts.fees}
    rows: list[dict[str, Any]] = []
    for fill in artifacts.fills:
        if fill.fill_id not in fill_ids:
            continue
        fee = fees.get(fill.fill_id)
        rows.append(
            {
                "symbol": compiled.instrument_symbols[fill.instrument_id],
                "instrument_id": fill.instrument_id,
                "side": fill.side.value,
                "shares": int(fill.quantity.to_decimal()),
                "price": float(fill.price.to_decimal()),
                "fee": float(fee.amount.to_decimal()) if fee is not None else 0.0,
                "fill_id": fill.fill_id,
                "order_id": fill.order_id,
                "event_time": fill.event_time.isoformat(),
                "source": "quant-execution actual fill",
            }
        )
    return rows


def _csv_bytes(fieldnames: list[str], rows: list[dict[str, object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _projection_bytes(
    compiled: CompiledReplay,
    log: dict[str, Any],
    portfolio: PortfolioState,
    trades: list[dict[str, Any]],
) -> dict[str, bytes]:
    portfolio_payload = portfolio.to_dict()
    artifacts = artifact_payloads(compiled)
    run_artifacts = compiled.runtime.engine.artifacts
    result = execution_payload(run_artifacts.result) if run_artifacts is not None else None
    portfolio_payload["_execution"] = {
        "authoritative_log": "execution_log.json",
        "authoritative_sha256": log["content_sha256"],
        "quant_execution_version": log["quant_execution_version"],
        "market_data_mode": "paper_research_close_not_live",
        "run_result": result,
    }
    nav_rows: list[dict[str, object]] = []
    for step, view in zip(log["steps"], compiled.steps, strict=True):
        snapshot = view.snapshot
        cash = snapshot.cash_balances.get("CNY")
        nav_rows.append(
            {
                "date": step["as_of"],
                "nav": format(snapshot.nav.to_decimal(), "f"),
                "cash": format(cash.to_decimal() if cash is not None else Decimal(0), "f"),
            }
        )
    holding_rows: list[dict[str, object]] = []
    for holding in portfolio.holdings:
        market_value = Decimal(holding.shares) * Decimal(str(holding.price))
        holding_rows.append(
            {
                "symbol": holding.symbol,
                "shares": holding.shares,
                "price": format(Decimal(str(holding.price)), "f"),
                "market_value": format(market_value, "f"),
                "weight": (
                    format(market_value / Decimal(str(portfolio.nav)), "f")
                    if portfolio.nav > 0
                    else "0"
                ),
            }
        )
    return {
        "portfolio.json": json.dumps(portfolio_payload, indent=2, ensure_ascii=False).encode(
            "utf-8"
        ),
        "nav.csv": _csv_bytes(["date", "nav", "cash"], nav_rows),
        "holdings.csv": _csv_bytes(
            ["symbol", "shares", "price", "market_value", "weight"], holding_rows
        ),
        "trades.json": json.dumps(trades, indent=2, ensure_ascii=False).encode("utf-8"),
        "execution_artifacts.json": json.dumps(
            {"authoritative_sha256": log["content_sha256"], **artifacts},
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8"),
    }


def _commit(
    paths: StatePaths,
    log: dict[str, Any],
    compiled: CompiledReplay,
    portfolio: PortfolioState,
    trades: list[dict[str, Any]],
) -> None:
    sealed = validate_log(log)
    projections = _projection_bytes(compiled, sealed, portfolio, trades)
    paths.root.mkdir(parents=True, exist_ok=True)
    pending = {
        "schema": "quant-paper-commit/1.0.0",
        "target_authoritative_sha256": sealed["content_sha256"],
        "files": sorted(projections),
    }
    atomic_write_json(paths.pending, pending)
    for name, data in projections.items():
        atomic_write_bytes(paths.root / name, data)
    manifest = {
        "schema": "quant-paper-projections/1.0.0",
        "authoritative_sha256": sealed["content_sha256"],
        "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in sorted(projections.items())
        },
    }
    atomic_write_json(paths.projection_manifest, manifest)
    atomic_write_json(paths.log, sealed)
    paths.pending.unlink(missing_ok=True)


def _legacy_cash_only_log(
    paths: StatePaths, cfg: dict[str, Any], *, initial_capital: float
) -> dict[str, Any]:
    if not paths.portfolio.is_file():
        raise StateError(
            "authoritative execution log is missing; run quant-paper init --config <path>"
        )
    try:
        legacy = json.loads(paths.portfolio.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"legacy portfolio cannot be read for migration: {exc}") from exc
    holdings = legacy.get("holdings") or []
    if any(int(row.get("shares", 0)) != 0 for row in holdings):
        raise StateError(
            "legacy portfolio contains holdings and cannot be migrated losslessly; "
            "archive the state directory, then run quant-paper init"
        )
    return new_log(
        initial_cash=legacy.get("cash", initial_capital),
        execution_config=_execution_config(cfg),
        migration={
            "kind": "legacy_cash_only",
            "source": "portfolio.json",
            "source_sha256": file_sha256(paths.portfolio),
        },
    )


def _load_or_migrate(
    paths: StatePaths,
    cfg: dict[str, Any],
    *,
    initial_capital: float,
    allow_fresh_step_bootstrap: bool = False,
) -> dict[str, Any]:
    if paths.pending.is_file():
        try:
            pending = json.loads(paths.pending.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"damaged pending commit marker {PENDING_FILE}: {exc}") from exc
        if not isinstance(pending, dict) or pending.get("schema") != "quant-paper-commit/1.0.0":
            raise StateError("damaged pending commit marker")
    if paths.log.is_file():
        log = load_log(paths.log)
        _assert_config_compatible(log, cfg)
        return log
    if allow_fresh_step_bootstrap and not paths.portfolio.is_file():
        return new_log(
            initial_cash=initial_capital,
            execution_config=_execution_config(cfg),
            migration={"kind": "fresh_step_bootstrap", "source": "quant-paper step"},
        )
    return _legacy_cash_only_log(paths, cfg, initial_capital=initial_capital)


def initialize(config_path: Path) -> RebalanceResult:
    from quant_paper_sim.readers.signals import load_config

    cfg = load_config(config_path)
    paths = _state_paths(config_path, cfg)
    initial_capital = float(cfg.get("initial_capital", 100_000))
    log = new_log(initial_cash=initial_capital, execution_config=_execution_config(cfg))
    compiled = _compile(log)
    portfolio = _portfolio_from(compiled, log, "init")
    result = RebalanceResult(portfolio=portfolio, trades=[])
    _commit(paths, log, compiled, portfolio, [])
    return result


def status(config_path: Path) -> PortfolioState:
    from quant_paper_sim.readers.signals import load_config

    cfg = load_config(config_path)
    paths = _state_paths(config_path, cfg)
    initial_capital = float(cfg.get("initial_capital", 100_000))
    log = _load_or_migrate(paths, cfg, initial_capital=initial_capital)
    compiled = _compile(log)
    as_of = log["steps"][-1]["as_of"] if log["steps"] else "init"
    portfolio = _portfolio_from(compiled, log, as_of)
    trades = _latest_trades(compiled)
    _commit(paths, log, compiled, portfolio, trades)
    return portfolio


def run_step(config_path: Path) -> RebalanceResult:
    from quant_paper_sim.readers.signals import load_config, load_signals

    cfg = load_config(config_path)
    paths = _state_paths(config_path, cfg)
    initial_capital = float(cfg.get("initial_capital", 100_000))
    log = _load_or_migrate(
        paths,
        cfg,
        initial_capital=initial_capital,
        allow_fresh_step_bootstrap=True,
    )
    source = _source_provenance(cfg, config_path)
    signals = load_signals(cfg, config_path)
    step = _step_record(signals, source=source)

    matching = [item for item in log["steps"] if item["as_of"] == step["as_of"]]
    if matching:
        if matching[0]["input_sha256"] != step["input_sha256"]:
            raise StateError("conflicting paper step content for the same as_of")
        compiled = _compile(log)
        portfolio = _portfolio_from(compiled, log, step["as_of"])
        trades = _latest_trades(compiled)
        _commit(paths, log, compiled, portfolio, trades)
        return RebalanceResult(portfolio=portfolio, trades=trades)
    if log["steps"] and step["as_of"] <= log["steps"][-1]["as_of"]:
        raise StateError("paper steps must be appended in strictly increasing as_of order")

    candidate = deepcopy(log)
    candidate["steps"].append(step)
    candidate = seal_log(candidate)
    compiled = compile_log(candidate)
    artifacts = compiled.runtime.engine.artifacts
    if artifacts is None:
        raise StateError("quant-execution did not produce artifacts")
    candidate["steps"][-1]["replay_evidence"] = {
        "fill_ids": list(compiled.steps[-1].fill_ids),
        "event_ids": list(compiled.steps[-1].event_ids),
    }
    candidate["steps"][-1]["cumulative_result"] = execution_payload(artifacts.result)
    candidate = seal_log(candidate)
    compiled = _compile(candidate)
    portfolio = _portfolio_from(compiled, candidate, step["as_of"])
    trades = _latest_trades(compiled)
    _commit(paths, candidate, compiled, portfolio, trades)
    return RebalanceResult(portfolio=portfolio, trades=trades)


def rebalance(
    portfolio: PortfolioState,
    signals: SignalBundle,
    *,
    commission_rate: float = 0.0003,
) -> RebalanceResult:
    """Compatibility API; cash-only input is executed through quant-execution."""

    if portfolio.holdings:
        raise StateError(
            "rebalance compatibility input with holdings cannot be migrated losslessly; "
            "use run_step with an authoritative execution log"
        )
    initial = portfolio.cash or portfolio.initial_capital
    log = new_log(
        initial_cash=initial,
        execution_config={
            "commission_rate": format(Decimal(str(commission_rate)), "f"),
            "stamp_duty_rate": "0.0005",
            "price_scale": 2,
            "money_scale": MONEY_SCALE,
            "bar_participation_rate": "1",
            "slippage_ticks": 0,
            "seed": 42,
            "market_data_mode": "paper_research_close_not_live",
            "stale_exit_price_policy": "last_known_signal_close",
        },
    )
    step = _step_record(
        signals,
        source={
            "kind": "python_api",
            "path": "in_memory",
            "sha256": sha256_payload(signals.to_dict()),
            "market_data_semantics": "paper_research_close_not_live",
        },
    )
    log["steps"].append(step)
    log = seal_log(log)
    compiled = _compile(log)
    result_portfolio = _portfolio_from(compiled, log, step["as_of"])
    return RebalanceResult(portfolio=result_portfolio, trades=_latest_trades(compiled))


def load_portfolio(path: Path, initial_capital: float) -> PortfolioState:
    """Read a legacy projection only; never used to restore authoritative execution state."""

    if not path.is_file():
        return PortfolioState(as_of="", cash=initial_capital, initial_capital=initial_capital)
    data = json.loads(path.read_text(encoding="utf-8"))
    holdings = [
        Holding(
            symbol=str(row["symbol"]),
            shares=int(row["shares"]),
            price=float(row["price"]),
        )
        for row in data.get("holdings") or []
    ]
    return PortfolioState(
        as_of=str(data.get("as_of", "")),
        cash=float(data.get("cash", initial_capital)),
        holdings=holdings,
        initial_capital=float(data.get("initial_capital", initial_capital)),
        authoritative=False,
        execution_log_sha256="",
    )


def save_portfolio(path: Path, portfolio: PortfolioState) -> None:
    """Compatibility projection writer; authoritative CLI state uses `_commit`."""

    atomic_write_json(path, portfolio.to_dict())


def append_nav(nav_path: Path, portfolio: PortfolioState) -> None:
    rows: list[dict[str, object]] = []
    if nav_path.is_file():
        with nav_path.open(encoding="utf-8", newline="") as handle:
            rows.extend(csv.DictReader(handle))
    rows.append({"date": portfolio.as_of, "nav": portfolio.nav, "cash": portfolio.cash})
    atomic_write_bytes(nav_path, _csv_bytes(["date", "nav", "cash"], rows))


def write_holdings_csv(path: Path, portfolio: PortfolioState) -> None:
    rows = [
        {
            "symbol": holding.symbol,
            "shares": holding.shares,
            "price": holding.price,
            "market_value": holding.market_value,
            "weight": holding.market_value / portfolio.nav if portfolio.nav > 0 else 0,
        }
        for holding in portfolio.holdings
    ]
    atomic_write_bytes(
        path,
        _csv_bytes(["symbol", "shares", "price", "market_value", "weight"], rows),
    )
