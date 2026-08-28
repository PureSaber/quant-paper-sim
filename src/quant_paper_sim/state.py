"""Versioned authoritative paper-execution log and atomic file primitives."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant_execution import __version__ as execution_version

from quant_paper_sim import __version__ as paper_sim_version

LOG_SCHEMA = "quant-paper-execution-log/1.0.0"
LOG_FILE = "execution_log.json"
PROJECTION_MANIFEST_FILE = "projection_manifest.json"
EXECUTION_ARTIFACTS_FILE = "execution_artifacts.json"
PENDING_FILE = ".paper_commit_pending.json"


class StateError(RuntimeError):
    """The persisted paper state cannot be trusted or safely migrated."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_payload(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decimal_text(value: object, field: str, *, positive: bool = False) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StateError(f"{field} must be a finite decimal") from exc
    if not number.is_finite() or (positive and number <= 0):
        qualifier = "positive " if positive else "finite "
        raise StateError(f"{field} must be a {qualifier}decimal")
    return format(number, "f")


def new_log(
    *,
    initial_cash: object,
    execution_config: dict[str, object],
    migration: dict[str, object] | None = None,
) -> dict[str, Any]:
    if execution_version != "0.2.0":
        raise StateError(
            f"quant-execution version mismatch: expected 0.2.0, imported {execution_version}"
        )
    payload: dict[str, Any] = {
        "schema": LOG_SCHEMA,
        "quant_paper_sim_version": paper_sim_version,
        "quant_execution_version": execution_version,
        "account_id": "paper-account",
        "strategy_id": "paper-rebalance-v2",
        "base_currency": "CNY",
        "initial_cash": _decimal_text(initial_cash, "initial_cash", positive=True),
        "execution_config": execution_config,
        "steps": [],
    }
    if migration is not None:
        payload["migration"] = migration
    return seal_log(payload)


def seal_log(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = deepcopy(payload)
    sealed.pop("content_sha256", None)
    sealed["content_sha256"] = sha256_payload(sealed)
    return sealed


def step_input_payload(step: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "as_of",
        "trading_day",
        "targets",
        "regime_scale",
        "cash_reserve",
    )
    return {key: step[key] for key in keys}


def validate_log(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StateError("authoritative execution log must be a JSON object")
    required = {
        "schema",
        "quant_paper_sim_version",
        "quant_execution_version",
        "account_id",
        "strategy_id",
        "base_currency",
        "initial_cash",
        "execution_config",
        "steps",
        "content_sha256",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise StateError(f"authoritative execution log is missing fields: {missing}")
    if payload["schema"] != LOG_SCHEMA:
        raise StateError(f"unsupported authoritative log schema: {payload['schema']!r}")
    if paper_sim_version != "0.2.0" or payload["quant_paper_sim_version"] != "0.2.0":
        raise StateError("authoritative log requires quant-paper-sim 0.2.0")
    if execution_version != "0.2.0":
        raise StateError(
            f"quant-execution version mismatch: expected 0.2.0, imported {execution_version}"
        )
    if payload["quant_execution_version"] != "0.2.0":
        raise StateError("authoritative log requires quant-execution 0.2.0")
    if (
        payload["account_id"] != "paper-account"
        or payload["strategy_id"] != "paper-rebalance-v2"
        or payload["base_currency"] != "CNY"
    ):
        raise StateError("authoritative execution identity is not supported")
    expected = seal_log(payload)["content_sha256"]
    if payload["content_sha256"] != expected:
        raise StateError("authoritative execution log checksum mismatch")
    if not isinstance(payload["execution_config"], dict):
        raise StateError("execution_config must be an object")
    if not isinstance(payload["steps"], list):
        raise StateError("steps must be an array")
    _decimal_text(payload["initial_cash"], "initial_cash", positive=True)
    prior_as_of = ""
    seen: set[str] = set()
    for index, step in enumerate(payload["steps"]):
        if not isinstance(step, dict):
            raise StateError(f"steps[{index}] must be an object")
        step_required = {
            "as_of",
            "trading_day",
            "input_sha256",
            "signal_source",
            "targets",
            "regime_scale",
            "cash_reserve",
        }
        step_missing = sorted(step_required - step.keys())
        if step_missing:
            raise StateError(f"steps[{index}] is missing fields: {step_missing}")
        as_of = str(step["as_of"])
        if as_of in seen or (prior_as_of and as_of <= prior_as_of):
            raise StateError("authoritative step timestamps must be unique and strictly increasing")
        seen.add(as_of)
        prior_as_of = as_of
        if step["input_sha256"] != sha256_payload(step_input_payload(step)):
            raise StateError(f"steps[{index}] input checksum mismatch")
        if not isinstance(step["targets"], list) or not step["targets"]:
            raise StateError(f"steps[{index}].targets must be a non-empty array")
    return deepcopy(payload)


def load_log(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read authoritative execution log {path}: {exc}") from exc
    return validate_log(payload)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_bytes(path, json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))


@dataclass(frozen=True)
class StatePaths:
    root: Path

    @property
    def log(self) -> Path:
        return self.root / LOG_FILE

    @property
    def portfolio(self) -> Path:
        return self.root / "portfolio.json"

    @property
    def nav(self) -> Path:
        return self.root / "nav.csv"

    @property
    def holdings(self) -> Path:
        return self.root / "holdings.csv"

    @property
    def trades(self) -> Path:
        return self.root / "trades.json"

    @property
    def projection_manifest(self) -> Path:
        return self.root / PROJECTION_MANIFEST_FILE

    @property
    def execution_artifacts(self) -> Path:
        return self.root / EXECUTION_ARTIFACTS_FILE

    @property
    def pending(self) -> Path:
        return self.root / PENDING_FILE
