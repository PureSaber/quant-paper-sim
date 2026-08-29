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
CURRENT_PAPER_SIM_VERSION = "0.2.1"
CURRENT_EXECUTION_VERSION = "0.4.1"
HISTORICAL_PAPER_SIM_VERSION = "0.2.0"
HISTORICAL_EXECUTION_VERSION = "0.2.0"
CURRENT_REPLAY_PROFILE = "quant-execution/0.4.1-causal-opening"
HISTORICAL_REPLAY_PROFILE = "quant-execution/0.2.0-frozen-opening"
COMPAT_MIGRATION_KIND = "quant_execution_0_2_0_to_0_4_1_compat"
LOG_FILE = "execution_log.json"
PROJECTION_MANIFEST_FILE = "projection_manifest.json"
EXECUTION_ARTIFACTS_FILE = "execution_artifacts.json"
PENDING_FILE = ".paper_commit_pending.json"


class StateError(RuntimeError):
    """The persisted paper state cannot be trusted or safely migrated."""


def _require_current_runtime() -> None:
    if paper_sim_version != CURRENT_PAPER_SIM_VERSION:
        raise StateError(
            "quant-paper-sim runtime version mismatch: "
            f"expected {CURRENT_PAPER_SIM_VERSION}, imported {paper_sim_version}"
        )
    if execution_version != CURRENT_EXECUTION_VERSION:
        raise StateError(
            "quant-execution runtime version mismatch: "
            f"expected {CURRENT_EXECUTION_VERSION}, imported {execution_version}"
        )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def is_historical_log(payload: dict[str, Any]) -> bool:
    return (
        payload.get("schema") == LOG_SCHEMA
        and payload.get("quant_paper_sim_version") == HISTORICAL_PAPER_SIM_VERSION
        and payload.get("quant_execution_version") == HISTORICAL_EXECUTION_VERSION
    )


def is_compat_migrated_log(payload: dict[str, Any]) -> bool:
    migration = payload.get("migration")
    return (
        payload.get("schema") == LOG_SCHEMA
        and payload.get("quant_paper_sim_version") == CURRENT_PAPER_SIM_VERSION
        and payload.get("quant_execution_version") == CURRENT_EXECUTION_VERSION
        and isinstance(migration, dict)
        and migration.get("kind") == COMPAT_MIGRATION_KIND
    )


def _validate_compat_migration(payload: dict[str, Any]) -> None:
    migration = payload.get("migration")
    required = {
        "kind",
        "replay_profile",
        "source_schema",
        "source_quant_paper_sim_version",
        "source_quant_execution_version",
        "source_content_sha256",
        "source_file_sha256",
        "source_archive",
    }
    if not isinstance(migration, dict) or set(migration) != required:
        raise StateError("compatibility migration metadata is malformed")
    expected = {
        "kind": COMPAT_MIGRATION_KIND,
        "replay_profile": HISTORICAL_REPLAY_PROFILE,
        "source_schema": LOG_SCHEMA,
        "source_quant_paper_sim_version": HISTORICAL_PAPER_SIM_VERSION,
        "source_quant_execution_version": HISTORICAL_EXECUTION_VERSION,
    }
    if any(migration.get(key) != value for key, value in expected.items()):
        raise StateError("compatibility migration metadata is not supported")
    if not _is_sha256(migration.get("source_content_sha256")) or not _is_sha256(
        migration.get("source_file_sha256")
    ):
        raise StateError("compatibility migration hashes are malformed")
    expected_archive = f"execution_log.v0.2.0.{migration['source_content_sha256']}.json"
    if migration.get("source_archive") != expected_archive:
        raise StateError("compatibility migration archive name is not canonical")


def replay_profile(payload: dict[str, Any]) -> str:
    versions = (
        payload.get("schema"),
        payload.get("quant_paper_sim_version"),
        payload.get("quant_execution_version"),
    )
    historical = (LOG_SCHEMA, HISTORICAL_PAPER_SIM_VERSION, HISTORICAL_EXECUTION_VERSION)
    current = (LOG_SCHEMA, CURRENT_PAPER_SIM_VERSION, CURRENT_EXECUTION_VERSION)
    if versions == historical:
        return HISTORICAL_REPLAY_PROFILE
    if versions == current:
        if is_compat_migrated_log(payload):
            _validate_compat_migration(payload)
            return HISTORICAL_REPLAY_PROFILE
        return CURRENT_REPLAY_PROFILE
    raise StateError(
        "unsupported authoritative log version tuple: "
        f"schema={versions[0]!r}, quant-paper-sim={versions[1]!r}, "
        f"quant-execution={versions[2]!r}"
    )


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
    _require_current_runtime()
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
    _require_current_runtime()
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
    replay_profile(payload)
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


def migrate_historical_log(payload: dict[str, Any], *, source_file_sha256: str) -> dict[str, Any]:
    historical = validate_log(payload)
    if not is_historical_log(historical):
        raise StateError("only an exact quant-paper-sim 0.2.0 log can use compatibility migration")
    if not _is_sha256(source_file_sha256):
        raise StateError("historical authoritative log file hash is malformed")
    source_content_sha256 = historical["content_sha256"]
    migrated = deepcopy(historical)
    migrated["quant_paper_sim_version"] = CURRENT_PAPER_SIM_VERSION
    migrated["quant_execution_version"] = CURRENT_EXECUTION_VERSION
    migrated["migration"] = {
        "kind": COMPAT_MIGRATION_KIND,
        "replay_profile": HISTORICAL_REPLAY_PROFILE,
        "source_schema": LOG_SCHEMA,
        "source_quant_paper_sim_version": HISTORICAL_PAPER_SIM_VERSION,
        "source_quant_execution_version": HISTORICAL_EXECUTION_VERSION,
        "source_content_sha256": source_content_sha256,
        "source_file_sha256": source_file_sha256,
        "source_archive": f"execution_log.v0.2.0.{source_content_sha256}.json",
    }
    return seal_log(migrated)


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
