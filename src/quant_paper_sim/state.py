"""Versioned authoritative paper-execution log and atomic file primitives."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quant_execution import __version__ as execution_version

from quant_paper_sim import __version__ as paper_sim_version

LOG_SCHEMA = "quant-paper-execution-log/1.0.0"
CURRENT_PAPER_SIM_VERSION = "0.2.2"
CURRENT_EXECUTION_VERSION = "0.5.1"
PREVIOUS_PAPER_SIM_VERSION = "0.2.1"
PREVIOUS_EXECUTION_VERSION = "0.4.1"
HISTORICAL_PAPER_SIM_VERSION = "0.2.0"
HISTORICAL_EXECUTION_VERSION = "0.2.0"
CURRENT_REPLAY_PROFILE = "quant-execution/0.5.1-causal-opening"
HISTORICAL_REPLAY_PROFILE = "quant-execution/0.2.0-frozen-opening"
PREVIOUS_COMPAT_MIGRATION_KIND = "quant_execution_0_2_0_to_0_4_1_compat"
COMPAT_MIGRATION_KIND = "quant_paper_authority_to_0_2_2_compat"
LOG_FILE = "execution_log.json"
PROJECTION_MANIFEST_FILE = "projection_manifest.json"
EXECUTION_ARTIFACTS_FILE = "execution_artifacts.json"
PENDING_FILE = ".paper_commit_pending.json"


class StateError(RuntimeError):
    """The persisted paper state cannot be trusted or safely migrated."""


_WINDOWS_REPARSE_POINT = 0x0400
_WINDOWS_MOVEFILE_REPLACE_EXISTING = 0x00000001
_WINDOWS_MOVEFILE_WRITE_THROUGH = 0x00000008


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT)


def _require_regular_stat(path: Path, info: os.stat_result, purpose: str) -> None:
    if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise StateError(f"{purpose} is not a regular file under no-follow semantics: {path}")


def _regular_lstat(path: Path, purpose: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StateError(f"cannot inspect {purpose} {path}: {exc}") from exc
    _require_regular_stat(path, info, purpose)
    return info


def regular_file_exists(path: Path, purpose: str) -> bool:
    """Return whether path is a real regular file, rejecting links and reparse points."""

    try:
        _regular_lstat(path, purpose)
    except FileNotFoundError:
        return False
    return True


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    try:
        return os.path.samestat(left, right)
    except (AttributeError, OSError):
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_regular_descriptor(path: Path, purpose: str, *, writable: bool = False) -> int:
    before = _regular_lstat(path, purpose)
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StateError(f"cannot open {purpose} without following links {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        _require_regular_stat(path, opened, purpose)
        after = _regular_lstat(path, purpose)
        if not _same_file(before, opened) or not _same_file(opened, after):
            raise StateError(f"{purpose} identity changed while opening: {path}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


@dataclass
class VerifiedRegularFile:
    """A no-follow regular file whose descriptor and canonical path remain identical."""

    path: Path
    purpose: str
    descriptor: int
    expected_bytes: bytes | None = None

    def read_and_verify(self) -> bytes:
        opened = os.fstat(self.descriptor)
        _require_regular_stat(self.path, opened, self.purpose)
        current = _regular_lstat(self.path, self.purpose)
        if not _same_file(opened, current):
            raise StateError(f"{self.purpose} identity changed before commit: {self.path}")
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            block = os.read(self.descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        data = b"".join(chunks)
        after_read = os.fstat(self.descriptor)
        after_path = _regular_lstat(self.path, self.purpose)
        if not _same_file(opened, after_read) or not _same_file(after_read, after_path):
            raise StateError(f"{self.purpose} identity changed while verifying: {self.path}")
        if self.expected_bytes is not None and data != self.expected_bytes:
            raise StateError(f"{self.purpose} content changed before commit: {self.path}")
        return data

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self):
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


def open_verified_regular(
    path: Path,
    purpose: str,
    *,
    expected_bytes: bytes | None = None,
    writable: bool = False,
) -> VerifiedRegularFile:
    verified = VerifiedRegularFile(
        path=path,
        purpose=purpose,
        descriptor=_open_regular_descriptor(path, purpose, writable=writable),
        expected_bytes=expected_bytes,
    )
    try:
        verified.read_and_verify()
    except Exception:
        verified.close()
        raise
    return verified


def read_regular_bytes(path: Path, purpose: str) -> bytes:
    with open_verified_regular(path, purpose) as verified:
        return verified.read_and_verify()


def _sync_parent_directory(directory: Path) -> None:
    """Persist prior POSIX directory-entry changes; Windows uses write-through moves."""

    if os.name == "nt":  # pragma: no branch - mutually exclusive platform implementation
        raise StateError(
            "directory fsync is unavailable on Windows; a write-through move is required"
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_move_file(source: Path, destination: Path, *, replace: bool) -> None:
    import ctypes

    flags = _WINDOWS_MOVEFILE_WRITE_THROUGH
    if replace:
        flags |= _WINDOWS_MOVEFILE_REPLACE_EXISTING
    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file.restype = ctypes.c_int
    if move_file(str(source), str(destination), flags):
        return
    error = ctypes.get_last_error()
    if not replace and error in {80, 183}:
        raise FileExistsError(error, os.strerror(error), str(destination))
    raise OSError(error, os.strerror(error), str(destination))


def _durable_replace(
    source: Path,
    destination: Path,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    if before_replace is not None:
        before_replace()
    if os.name == "nt":  # pragma: no branch - mutually exclusive platform implementation
        _windows_move_file(source, destination, replace=True)
        return
    os.replace(source, destination)
    _sync_parent_directory(destination.parent)


def _install_new_file(source: Path, destination: Path) -> None:
    """Install a new canonical file without ever replacing an existing path."""

    if os.name == "nt":  # pragma: no branch - mutually exclusive platform implementation
        _windows_move_file(source, destination, replace=False)
        return
    os.link(source, destination, follow_symlinks=False)
    _sync_parent_directory(destination.parent)
    source.unlink()
    _sync_parent_directory(destination.parent)


def install_or_open_archive(path: Path, data: bytes) -> VerifiedRegularFile:
    """Create a durable archive exclusively or verify the exact existing regular file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if not regular_file_exists(path, "historical authoritative log archive"):
        descriptor, raw_temp = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".archive.tmp", dir=path.parent
        )
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                _install_new_file(temp_path, path)
            except FileExistsError:
                # Another conforming writer won the exclusive install; verify its exact bytes.
                pass
        finally:
            temp_path.unlink(missing_ok=True)
    verified = open_verified_regular(
        path,
        "historical authoritative log archive",
        expected_bytes=data,
        writable=True,
    )
    try:
        os.fsync(verified.descriptor)
        if os.name != "nt":  # pragma: no branch - mutually exclusive platform implementation
            _sync_parent_directory(path.parent)
        verified.read_and_verify()
    except Exception:
        verified.close()
        raise
    return verified


def durable_unlink(path: Path) -> None:
    """Durably clear a marker without following it."""

    try:
        path.lstat()
    except FileNotFoundError:
        return
    if os.name == "nt":  # pragma: no branch - mutually exclusive platform implementation
        tombstone = path.with_name(f".{path.name}.cleared")
        _windows_move_file(path, tombstone, replace=True)
        tombstone.unlink(missing_ok=True)
        return
    path.unlink()
    _sync_parent_directory(path.parent)


@contextmanager
def state_writer_lock(root: Path) -> Iterator[None]:
    """Serialize state writers and reject lock-path links/reparse points."""

    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".paper_state.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise StateError(f"cannot safely open paper state writer lock: {exc}") from exc
    locked = False
    try:
        opened = os.fstat(descriptor)
        _require_regular_stat(lock_path, opened, "paper state writer lock")
        current = _regular_lstat(lock_path, "paper state writer lock")
        if not _same_file(opened, current):
            raise StateError("paper state writer lock identity changed while opening")
        if os.name == "nt":  # pragma: no branch - mutually exclusive platform implementation
            import msvcrt

            if opened.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise StateError("paper state is already being modified") from exc
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StateError("paper state is already being modified") from exc
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":  # pragma: no branch - mutually exclusive platform implementation
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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


def is_previous_log(payload: dict[str, Any]) -> bool:
    return (
        payload.get("schema") == LOG_SCHEMA
        and payload.get("quant_paper_sim_version") == PREVIOUS_PAPER_SIM_VERSION
        and payload.get("quant_execution_version") == PREVIOUS_EXECUTION_VERSION
    )


def requires_compat_migration(payload: dict[str, Any]) -> bool:
    return is_historical_log(payload) or is_previous_log(payload)


def is_compat_migrated_log(payload: dict[str, Any]) -> bool:
    migration = payload.get("migration")
    if not isinstance(migration, dict) or payload.get("schema") != LOG_SCHEMA:
        return False
    versions = (
        payload.get("quant_paper_sim_version"),
        payload.get("quant_execution_version"),
        migration.get("kind"),
    )
    return versions in {
        (
            PREVIOUS_PAPER_SIM_VERSION,
            PREVIOUS_EXECUTION_VERSION,
            PREVIOUS_COMPAT_MIGRATION_KIND,
        ),
        (CURRENT_PAPER_SIM_VERSION, CURRENT_EXECUTION_VERSION, COMPAT_MIGRATION_KIND),
    }


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
    versions = (
        payload.get("quant_paper_sim_version"),
        payload.get("quant_execution_version"),
    )
    previous = (PREVIOUS_PAPER_SIM_VERSION, PREVIOUS_EXECUTION_VERSION)
    current = (CURRENT_PAPER_SIM_VERSION, CURRENT_EXECUTION_VERSION)
    if versions == previous:
        expected = {
            "kind": PREVIOUS_COMPAT_MIGRATION_KIND,
            "replay_profile": HISTORICAL_REPLAY_PROFILE,
            "source_schema": LOG_SCHEMA,
            "source_quant_paper_sim_version": HISTORICAL_PAPER_SIM_VERSION,
            "source_quant_execution_version": HISTORICAL_EXECUTION_VERSION,
        }
    elif versions == current:
        expected = {
            "kind": COMPAT_MIGRATION_KIND,
            "source_schema": LOG_SCHEMA,
        }
        source_versions = (
            migration.get("source_quant_paper_sim_version"),
            migration.get("source_quant_execution_version"),
        )
        allowed_sources = {
            (HISTORICAL_PAPER_SIM_VERSION, HISTORICAL_EXECUTION_VERSION),
            (PREVIOUS_PAPER_SIM_VERSION, PREVIOUS_EXECUTION_VERSION),
        }
        if source_versions not in allowed_sources:
            raise StateError("compatibility migration source versions are not supported")
        allowed_profiles = {HISTORICAL_REPLAY_PROFILE}
        if source_versions == previous:
            allowed_profiles.add(CURRENT_REPLAY_PROFILE)
        if migration.get("replay_profile") not in allowed_profiles:
            raise StateError("compatibility migration replay profile is not supported")
    else:
        raise StateError("compatibility migration target versions are not supported")
    if any(migration.get(key) != value for key, value in expected.items()):
        raise StateError("compatibility migration metadata is not supported")
    if not _is_sha256(migration.get("source_content_sha256")) or not _is_sha256(
        migration.get("source_file_sha256")
    ):
        raise StateError("compatibility migration hashes are malformed")
    source_paper_version = migration["source_quant_paper_sim_version"]
    expected_archive = (
        f"execution_log.v{source_paper_version}.{migration['source_content_sha256']}.json"
    )
    if migration.get("source_archive") != expected_archive:
        raise StateError("compatibility migration archive name is not canonical")


def replay_profile(payload: dict[str, Any]) -> str:
    versions = (
        payload.get("schema"),
        payload.get("quant_paper_sim_version"),
        payload.get("quant_execution_version"),
    )
    historical = (LOG_SCHEMA, HISTORICAL_PAPER_SIM_VERSION, HISTORICAL_EXECUTION_VERSION)
    previous = (LOG_SCHEMA, PREVIOUS_PAPER_SIM_VERSION, PREVIOUS_EXECUTION_VERSION)
    current = (LOG_SCHEMA, CURRENT_PAPER_SIM_VERSION, CURRENT_EXECUTION_VERSION)
    if versions == historical:
        return HISTORICAL_REPLAY_PROFILE
    if versions == previous:
        if is_compat_migrated_log(payload):
            _validate_compat_migration(payload)
            return HISTORICAL_REPLAY_PROFILE
        return CURRENT_REPLAY_PROFILE
    if versions == current:
        if is_compat_migrated_log(payload):
            _validate_compat_migration(payload)
            return payload["migration"]["replay_profile"]
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
    return hashlib.sha256(read_regular_bytes(path, "state file")).hexdigest()


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


def migrate_compatible_log(payload: dict[str, Any], *, source_file_sha256: str) -> dict[str, Any]:
    source = validate_log(payload)
    if not requires_compat_migration(source):
        raise StateError("only an exact supported prior release can use compatibility migration")
    if not _is_sha256(source_file_sha256):
        raise StateError("prior authoritative log file hash is malformed")
    source_content_sha256 = source["content_sha256"]
    source_paper_version = source["quant_paper_sim_version"]
    source_execution_version = source["quant_execution_version"]
    source_profile = replay_profile(source)
    migrated = deepcopy(source)
    migrated["quant_paper_sim_version"] = CURRENT_PAPER_SIM_VERSION
    migrated["quant_execution_version"] = CURRENT_EXECUTION_VERSION
    migrated["migration"] = {
        "kind": COMPAT_MIGRATION_KIND,
        "replay_profile": source_profile,
        "source_schema": LOG_SCHEMA,
        "source_quant_paper_sim_version": source_paper_version,
        "source_quant_execution_version": source_execution_version,
        "source_content_sha256": source_content_sha256,
        "source_file_sha256": source_file_sha256,
        "source_archive": f"execution_log.v{source_paper_version}.{source_content_sha256}.json",
    }
    return seal_log(migrated)


def migrate_historical_log(payload: dict[str, Any], *, source_file_sha256: str) -> dict[str, Any]:
    """Backward-compatible name for migration from an allowlisted prior authority."""

    return migrate_compatible_log(payload, source_file_sha256=source_file_sha256)


def load_log(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(read_regular_bytes(path, "authoritative execution log"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read authoritative execution log {path}: {exc}") from exc
    return validate_log(payload)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(temp_path, path, before_replace=before_replace)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(
    path: Path,
    payload: object,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    atomic_write_bytes(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
        before_replace=before_replace,
    )


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
