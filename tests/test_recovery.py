import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from test_execution_state import paper_config, write_signal
from test_state_compatibility import compatibility_case

from quant_paper_sim import engine
from quant_paper_sim.cli import main
from quant_paper_sim.engine import run_step, status
from quant_paper_sim.recovery import backup_state, restore_state, verify_state
from quant_paper_sim.state import (
    HISTORICAL_REPLAY_PROFILE,
    LOG_SCHEMA,
    PREVIOUS_COMPAT_MIGRATION_KIND,
    PREVIOUS_EXECUTION_VERSION,
    PREVIOUS_PAPER_SIM_VERSION,
    StateError,
    seal_log,
    state_writer_lock,
)


def account(tmp_path):
    config, signal, state = paper_config(tmp_path)
    write_signal(signal, as_of="2025-03-03", targets=[("600519.SH", 0.5, 10)])
    expected = run_step(config).portfolio
    return config, signal, state, expected


def restored_config(config, destination):
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    cfg["state_dir"] = str(destination)
    restored = config.with_name("restored.yaml")
    restored.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return restored


def files(root):
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


def test_verify_is_read_only_and_detects_resigned_projection_corruption(tmp_path):
    config, _, state, expected = account(tmp_path)
    before = files(state)
    report = verify_state(config)
    assert report["replay_verified"] and report["projection_status"] == "complete"
    assert files(state) == before
    portfolio = state / "portfolio.json"
    payload = json.loads(portfolio.read_text())
    payload["cash"] += 100
    portfolio.write_text(json.dumps(payload), encoding="utf-8")
    manifest_path = state / "projection_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][portfolio.name] = hashlib.sha256(portfolio.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    damaged = files(state)
    report = verify_state(config)
    assert report["projection_status"] == "needs_rebuild"
    assert "portfolio.json" in report["mismatched_files"]
    assert files(state) == damaged
    assert status(config).to_dict() == expected.to_dict()
    assert verify_state(config)["projection_status"] == "complete"


def test_independent_backup_restores_exact_authority_without_signal_inputs(tmp_path):
    config, signal, state, expected = account(tmp_path)
    source = files(state)
    backup = tmp_path / "backup"
    saved = backup_state(config, backup)
    assert saved["steps"] == 1 and files(state) == source
    signal.unlink()
    destination = tmp_path / "recovered"
    restored = restored_config(config, destination)
    result = restore_state(restored, backup)
    assert result["projection_status"] == "complete"
    assert (destination / "execution_log.json").read_bytes() == source["execution_log.json"]
    assert status(restored).to_dict() == expected.to_dict()
    with pytest.raises((StateError, FileExistsError)):
        backup_state(config, backup)
    with pytest.raises(StateError, match="empty"):
        restore_state(restored, backup)


@pytest.mark.parametrize("damage", ["checksum", "missing", "manifest", "extra"])
def test_invalid_backup_is_rejected_before_destination_is_created(tmp_path, damage):
    config, _, _, _ = account(tmp_path)
    backup = tmp_path / "backup"
    backup_state(config, backup)
    if damage == "checksum":
        (backup / "execution_log.json").write_text("{}")
    elif damage == "missing":
        (backup / "execution_log.json").unlink()
    elif damage == "extra":
        (backup / "other.json").write_text("{}")
    else:
        manifest = json.loads((backup / "backup_manifest.json").read_text())
        manifest["files"]["../execution_log.json"] = "0" * 64
        (backup / "backup_manifest.json").write_text(json.dumps(manifest))
    destination = tmp_path / "recovered"
    with pytest.raises((StateError, OSError, ValueError)):
        restore_state(restored_config(config, destination), backup)
    assert not destination.exists()


def test_restore_projection_failure_keeps_authority_recoverable(tmp_path, monkeypatch):
    config, _, state, expected = account(tmp_path)
    backup = tmp_path / "backup"
    backup_state(config, backup)
    destination = tmp_path / "recovered"
    restored = restored_config(config, destination)
    original = engine.atomic_write_bytes

    def fail(path: Path, data, **kwargs):
        if path.name == "nav.csv":
            raise OSError("injected projection interruption")
        return original(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(engine, "atomic_write_bytes", fail)
        with pytest.raises(OSError, match="injected"):
            restore_state(restored, backup)
    assert (destination / "execution_log.json").read_bytes() == (
        state / "execution_log.json"
    ).read_bytes()
    assert (destination / ".paper_commit_pending.json").is_file()
    assert status(restored).to_dict() == expected.to_dict()


def test_backup_and_restore_respect_writer_lock_and_config_identity(tmp_path):
    config, _, state, _ = account(tmp_path)
    with state_writer_lock(state), pytest.raises(StateError, match="already"):
        backup_state(config, tmp_path / "locked-backup")
    assert not (tmp_path / "locked-backup").exists()
    backup = tmp_path / "backup"
    backup_state(config, backup)
    restored = restored_config(config, tmp_path / "recovered")
    cfg = yaml.safe_load(restored.read_text())
    cfg["commission_rate"] = 0.001
    restored.write_text(yaml.safe_dump(cfg))
    with pytest.raises(StateError, match="configuration"):
        restore_state(restored, backup)
    assert not (tmp_path / "recovered").exists()


def test_recovery_cli_round_trip_and_missing_authority_fail_closed(tmp_path, capsys):
    config, _, state, _ = account(tmp_path)
    main(["verify", "--config", str(config)])
    assert json.loads(capsys.readouterr().out)["replay_verified"]
    backup = tmp_path / "backup"
    main(["backup", "--config", str(config), "--out", str(backup)])
    assert json.loads(capsys.readouterr().out)["steps"] == 1
    restored = restored_config(config, tmp_path / "restored")
    main(["restore", "--config", str(restored), "--backup", str(backup)])
    assert json.loads(capsys.readouterr().out)["projection_status"] == "complete"
    (state / "execution_log.json").unlink()
    with pytest.raises((StateError, OSError)):
        backup_state(config, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_migrated_historical_archive_is_required_and_preserved_in_backup(tmp_path):
    config, _, state, _ = compatibility_case(tmp_path)
    expected = run_step(config).portfolio
    authority = files(state)
    backup = tmp_path / "historical-backup"
    saved = backup_state(config, backup)
    assert len(saved["files"]) == 2
    archive = next(backup.glob("execution_log.v*.json"))
    assert archive.read_bytes() == authority[archive.name]
    restored = restored_config(config, tmp_path / "recovered")
    restore_state(restored, backup)
    assert status(restored).to_dict() == expected.to_dict()
    archive.unlink()
    with pytest.raises(StateError, match="archive is missing"):
        restore_state(restored_config(config, tmp_path / "invalid"), backup)
    assert not (tmp_path / "invalid").exists()


def test_nested_release_archives_survive_independent_restore(tmp_path):
    config, _, state, historical_bytes = compatibility_case(tmp_path)
    historical = json.loads(historical_bytes)
    previous = {
        **historical,
        "quant_paper_sim_version": PREVIOUS_PAPER_SIM_VERSION,
        "quant_execution_version": PREVIOUS_EXECUTION_VERSION,
        "migration": {
            "kind": PREVIOUS_COMPAT_MIGRATION_KIND,
            "replay_profile": HISTORICAL_REPLAY_PROFILE,
            "source_schema": LOG_SCHEMA,
            "source_quant_paper_sim_version": "0.2.0",
            "source_quant_execution_version": "0.2.0",
            "source_content_sha256": historical["content_sha256"],
            "source_file_sha256": hashlib.sha256(historical_bytes).hexdigest(),
            "source_archive": f"execution_log.v0.2.0.{historical['content_sha256']}.json",
        },
    }
    previous = seal_log(previous)
    (state / previous["migration"]["source_archive"]).write_bytes(historical_bytes)
    (state / "execution_log.json").write_text(json.dumps(previous), encoding="utf-8")
    expected = run_step(config).portfolio
    backup = tmp_path / "nested-backup"
    saved = backup_state(config, backup)
    assert len(saved["files"]) == 3
    restored = restored_config(config, tmp_path / "recovered")
    restore_state(restored, backup)
    assert status(restored).to_dict() == expected.to_dict()
    for name in saved["files"]:
        assert (tmp_path / "recovered" / name).read_bytes() == (state / name).read_bytes()


@pytest.mark.parametrize("phase", ["before_authority", "after_authority"])
def test_killed_writer_releases_lock_and_retry_matches_uninterrupted_account(tmp_path, phase):
    config, signal, state, first = account(tmp_path)
    backup = tmp_path / "backup"
    backup_state(config, backup)
    reference_config = restored_config(config, tmp_path / "reference")
    restore_state(reference_config, backup)
    write_signal(signal, as_of="2025-03-04", targets=[("600519.SH", 0.3, 11)])
    expected = run_step(reference_config).portfolio
    ready = tmp_path / "writer-ready"
    code = """
import sys, time
from pathlib import Path
from quant_paper_sim import engine
real = engine.atomic_write_json
def pause(path, payload, **kwargs):
    if path.name == 'execution_log.json':
        if sys.argv[3] == 'after_authority':
            real(path, payload, **kwargs)
        Path(sys.argv[2]).write_text('ready')
        time.sleep(30)
    return real(path, payload, **kwargs)
engine.atomic_write_json = pause
engine.run_step(Path(sys.argv[1]))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(config), str(ready), phase],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "writer did not reach the authority commit boundary"
        with pytest.raises(StateError, match="already"):
            run_step(config)
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    assert (state / ".paper_commit_pending.json").is_file()
    assert status(config).to_dict() == (
        first.to_dict() if phase == "before_authority" else expected.to_dict()
    )
    assert run_step(config).portfolio.to_dict() == expected.to_dict()
    sealed = (state / "execution_log.json").read_bytes()
    assert run_step(config).portfolio.to_dict() == expected.to_dict()
    assert (state / "execution_log.json").read_bytes() == sealed
    assert verify_state(config)["projection_status"] == "complete"
