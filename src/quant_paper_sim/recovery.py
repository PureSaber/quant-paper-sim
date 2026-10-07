"""Read-only replay checks and independent, byte-preserving account backups."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .engine import (
    _assert_config_compatible,
    _commit,
    _compile,
    _latest_trades,
    _portfolio_from,
    _projection_bytes,
    _state_paths,
    _validate_pending_marker,
    _verify_migration_archive,
)
from .instrument_catalog import load_configured_catalog
from .readers.signals import load_config
from .state import (
    StateError,
    atomic_write_bytes,
    atomic_write_json,
    install_or_open_archive,
    is_compat_migrated_log,
    read_regular_bytes,
    regular_file_exists,
    state_writer_lock,
    validate_log,
)

MANIFEST = "backup_manifest.json"
BACKUP_SCHEMA = "quant-paper-backup/1.0.0"


def _configuration(config_path):
    cfg = load_config(config_path)
    return cfg, load_configured_catalog(cfg, config_path), _state_paths(config_path, cfg)


def _authority_files(paths):
    """Include the complete validated historical archive chain, without projections."""
    payloads = {"execution_log.json": read_regular_bytes(paths.log, "authoritative execution log")}
    log = validate_log(json.loads(payloads["execution_log.json"]))
    _verify_migration_archive(paths, log)
    current = log
    while is_compat_migrated_log(current):
        name = current["migration"]["source_archive"]
        if name in payloads:
            raise StateError("cyclic authoritative archive chain")
        payloads[name] = read_regular_bytes(paths.root / name, "compatibility archive")
        if hashlib.sha256(payloads[name]).hexdigest() != current["migration"]["source_file_sha256"]:
            raise StateError("compatibility archive changed while collecting backup evidence")
        current = validate_log(json.loads(payloads[name]))
    return log, payloads


def _replay(log, cfg, catalog):
    _assert_config_compatible(log, cfg, catalog)
    compiled = _compile(log, catalog)
    as_of = log["steps"][-1]["as_of"] if log["steps"] else "init"
    portfolio = _portfolio_from(compiled, log, as_of)
    return compiled, portfolio, _latest_trades(compiled)


def verify_state(config_path: Path) -> dict:
    """Inspect saved evidence without locks, migration, repair or directory creation."""
    cfg, catalog, paths = _configuration(config_path)
    log, payloads = _authority_files(paths)
    compiled, portfolio, trades = _replay(log, cfg, catalog)
    expected = _projection_bytes(compiled, log, portfolio, trades)
    manifest = {
        "schema": "quant-paper-projections/1.0.0",
        "authoritative_sha256": log["content_sha256"],
        "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in sorted(expected.items())
        },
    }
    expected["projection_manifest.json"] = json.dumps(
        manifest, indent=2, ensure_ascii=False
    ).encode("utf-8")
    mismatched = []
    for name, data in expected.items():
        path = paths.root / name
        if (
            not regular_file_exists(path, "account projection")
            or read_regular_bytes(path, "account projection") != data
        ):
            mismatched.append(name)
    pending = _validate_pending_marker(paths) is not None
    if (
        read_regular_bytes(paths.log, "authoritative execution log")
        != payloads["execution_log.json"]
    ):
        raise StateError("authoritative execution log changed during verification")
    return {
        "replay_verified": True,
        "authoritative_sha256": log["content_sha256"],
        "journal_file_sha256": hashlib.sha256(payloads["execution_log.json"]).hexdigest(),
        "steps": len(log["steps"]),
        "projection_status": "needs_rebuild" if mismatched or pending else "complete",
        "mismatched_files": sorted(mismatched),
        "pending_commit": pending,
        "portfolio": portfolio.to_dict(),
    }


def backup_state(config_path: Path, output: Path) -> dict:
    cfg, catalog, paths = _configuration(config_path)
    output = output.resolve()
    if output.is_relative_to(paths.root.resolve()) or paths.root.resolve().is_relative_to(output):
        raise StateError("backup must be independent of the account state directory")
    if not regular_file_exists(paths.log, "authoritative execution log"):
        raise StateError("cannot back up missing authoritative execution log")
    with state_writer_lock(paths.root):
        log, payloads = _authority_files(paths)
        _replay(log, cfg, catalog)
        output.mkdir(parents=True, exist_ok=False)
        for name, data in payloads.items():
            with install_or_open_archive(output / name, data):
                pass
        manifest = {
            "schema": BACKUP_SCHEMA,
            "authoritative_sha256": log["content_sha256"],
            "files": {
                name: hashlib.sha256(data).hexdigest() for name, data in sorted(payloads.items())
            },
        }
        # The last durable write marks a complete backup; a partial folder is rejected.
        atomic_write_json(output / MANIFEST, manifest)
    return {"backup": str(output), "steps": len(log["steps"]), **manifest}


def _load_backup(backup):
    manifest = json.loads(read_regular_bytes(backup / MANIFEST, "backup manifest"))
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema", "authoritative_sha256", "files"}
        or manifest["schema"] != BACKUP_SCHEMA
        or not isinstance(manifest["files"], dict)
    ):
        raise StateError("invalid backup manifest")
    # Validating the journal and each migration derives all permitted basenames.
    from .state import StatePaths

    log, payloads = _authority_files(StatePaths(backup))
    expected = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payloads.items())}
    if (
        manifest["authoritative_sha256"] != log["content_sha256"]
        or manifest["files"] != expected
        or {p.name for p in backup.iterdir()} != set(payloads) | {MANIFEST}
    ):
        raise StateError("backup file set or checksum mismatch")
    return log, payloads


def restore_state(config_path: Path, backup: Path) -> dict:
    """Restore verified authority to an empty account; never import projection balances."""
    cfg, catalog, paths = _configuration(config_path)
    log, payloads = _load_backup(backup)
    compiled, portfolio, trades = _replay(log, cfg, catalog)
    with state_writer_lock(paths.root):
        if any(p.name != ".paper_state.lock" for p in paths.root.iterdir()):
            raise StateError(
                "restore requires an empty state directory; preserve existing evidence"
            )
        for name, data in payloads.items():
            if name != "execution_log.json":
                with install_or_open_archive(paths.root / name, data):
                    pass
        # Install exact authority before projections so a later interruption is replay-repairable.
        atomic_write_bytes(paths.log, payloads["execution_log.json"])
        _commit(paths, log, compiled, portfolio, trades, write_authoritative=False)
    return verify_state(config_path)
