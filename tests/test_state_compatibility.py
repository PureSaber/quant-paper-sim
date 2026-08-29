from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from quant_execution import (
    LEGACY_SCHEMA_VERSION,
    RUN_RESULT_SCHEMA_ID,
    SCHEMA_VERSION,
    execution_payload,
    validate_json_record,
)

import quant_paper_sim.engine as paper_engine
import quant_paper_sim.state as paper_state
from quant_paper_sim.engine import initialize, run_step, status
from quant_paper_sim.execution import artifact_payloads, compile_log
from quant_paper_sim.instrument_catalog import (
    INSTRUMENT_RULE_PROFILE_VERSION,
    bundled_catalog_config,
)
from quant_paper_sim.state import (
    COMPAT_MIGRATION_KIND,
    CURRENT_EXECUTION_VERSION,
    CURRENT_PAPER_SIM_VERSION,
    StateError,
    load_log,
    migrate_historical_log,
    new_log,
    seal_log,
    sha256_payload,
    step_input_payload,
    validate_log,
)

# Execution hashes come from exact v0.2.0 releases; only the local source path was sanitized
# and the outer authoritative checksum resealed before committing the fixture.
FIXTURE = Path(__file__).parent / "fixtures" / "execution_log_v020.json"
HISTORICAL_RESULT_HASHES = {
    "order_sha256": "b838c29cd37ef66ea982a4d73e2b75c687a4673f7000868145c6e1b84eb99485",
    "fill_sha256": "e26617c9b37ea9245e2808c1887184231ac69ef51d4133e2aa37ac2b6bf9c309",
    "ledger_sha256": "41739ee5463ce11dfae634f692b26bfc76c1bfb668a6c0abc20585532dd0d8ff",
    "result_sha256": "6f4a5f79cc9ea2a8b5338fc751ccc8b2e0cce96965f19fa14b9a7483d3e5e9fd",
}


def compatibility_case(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    state = tmp_path / "state"
    state.mkdir()
    historical_bytes = FIXTURE.read_bytes()
    (state / "execution_log.json").write_bytes(historical_bytes)
    signal = tmp_path / "signal.yaml"
    signal.write_text(
        yaml.safe_dump(
            {
                "as_of": "2026-07-28",
                "cash_reserve": 0.05,
                "targets": [{"symbol": "000001", "weight": 1.0, "price": 10.0}],
            }
        ),
        encoding="utf-8",
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    config = config_dir / "paper.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(state),
                "initial_capital": 10_000,
                "cash_reserve": 0.05,
                "commission_rate": 0.0003,
                "stamp_duty_rate": 0.0005,
                "stale_exit_price_policy": "last_known_signal_close",
                "instrument_rule_profile": INSTRUMENT_RULE_PROFILE_VERSION,
                "instrument_catalog": bundled_catalog_config(),
                "signals": {"source": "yaml", "path": str(signal)},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config, signal, state, historical_bytes


def test_v020_log_replays_exact_hashes_and_status_never_rewrites_authority(
    tmp_path: Path,
) -> None:
    config, _, state, historical_bytes = compatibility_case(tmp_path)
    historical = load_log(state / "execution_log.json")
    compiled = compile_log(historical)
    artifacts = compiled.runtime.engine.artifacts
    assert artifacts is not None
    assert execution_payload(artifacts.result) == historical["steps"][-1]["cumulative_result"]
    assert {
        key: historical["steps"][-1]["cumulative_result"][key] for key in HISTORICAL_RESULT_HASHES
    } == HISTORICAL_RESULT_HASHES
    assert (
        list(compiled.steps[-1].fill_ids) == historical["steps"][-1]["replay_evidence"]["fill_ids"]
    )
    assert (
        list(compiled.steps[-1].event_ids)
        == historical["steps"][-1]["replay_evidence"]["event_ids"]
    )

    (state / "portfolio.json").write_text('{"cash": 777}', encoding="utf-8")
    results = [status(config).to_dict() for _ in range(3)]
    assert results[0] == results[1] == results[2]
    assert (state / "execution_log.json").read_bytes() == historical_bytes
    repaired = json.loads((state / "portfolio.json").read_text(encoding="utf-8"))
    assert repaired["cash"] != 777
    assert repaired["_execution"]["execution_schema_version"] == SCHEMA_VERSION


def test_v020_append_migrates_with_byte_archive_and_preserves_all_old_hashes(
    tmp_path: Path,
) -> None:
    config, signal, state, historical_bytes = compatibility_case(tmp_path)
    historical = json.loads(historical_bytes)
    original_result = deepcopy(historical["steps"][-1]["cumulative_result"])

    first = run_step(config)
    migrated = load_log(state / "execution_log.json")
    migration = migrated["migration"]
    archive = state / migration["source_archive"]
    assert first.portfolio.authoritative
    assert migrated["quant_paper_sim_version"] == CURRENT_PAPER_SIM_VERSION
    assert migrated["quant_execution_version"] == CURRENT_EXECUTION_VERSION
    assert migration["kind"] == COMPAT_MIGRATION_KIND
    assert archive.read_bytes() == historical_bytes
    assert migrated["steps"][0]["cumulative_result"] == original_result

    signal.write_text(
        yaml.safe_dump(
            {
                "as_of": "2026-07-29",
                "cash_reserve": 0.05,
                "targets": [{"symbol": "000001", "weight": 1.0, "price": 11.0}],
            }
        ),
        encoding="utf-8",
    )
    second = run_step(config)
    appended = load_log(state / "execution_log.json")
    assert second.portfolio.as_of.startswith("2026-07-29")
    assert len(appended["steps"]) == 2
    assert appended["steps"][0]["cumulative_result"] == original_result
    assert archive.read_bytes() == historical_bytes
    assert status(config).to_dict() == second.portfolio.to_dict()


def test_new_authoritative_state_records_current_versions(tmp_path: Path) -> None:
    config, _, state, _ = compatibility_case(tmp_path)
    (state / "execution_log.json").unlink()
    initialized = initialize(config)
    current = load_log(state / "execution_log.json")
    assert initialized.portfolio.authoritative
    assert current["quant_paper_sim_version"] == CURRENT_PAPER_SIM_VERSION
    assert current["quant_execution_version"] == CURRENT_EXECUTION_VERSION
    assert "migration" not in current


def test_migration_failure_keeps_historical_authority_and_repair_uses_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, state, historical_bytes = compatibility_case(tmp_path)
    real_write = paper_engine.atomic_write_json

    def fail_new_authority(path: Path, payload: object, **kwargs: object) -> None:
        if path.name == "execution_log.json":
            raise OSError("injected migration commit failure")
        real_write(path, payload, **kwargs)

    monkeypatch.setattr(paper_engine, "atomic_write_json", fail_new_authority)
    with pytest.raises(OSError, match="migration commit failure"):
        run_step(config)
    monkeypatch.setattr(paper_engine, "atomic_write_json", real_write)

    assert (state / "execution_log.json").read_bytes() == historical_bytes
    archive = next(iter(state.glob("execution_log.v0.2.0.*.json")))
    assert archive.read_bytes() == historical_bytes
    (state / "portfolio.json").write_text('{"cash": 1}', encoding="utf-8")
    status(config)
    assert (state / "execution_log.json").read_bytes() == historical_bytes
    assert json.loads((state / "portfolio.json").read_text(encoding="utf-8"))["cash"] != 1
    run_step(config)
    assert load_log(state / "execution_log.json")["migration"]["kind"] == COMPAT_MIGRATION_KIND


def test_migrated_log_requires_untampered_source_archive(tmp_path: Path) -> None:
    config, _, state, _ = compatibility_case(tmp_path)
    run_step(config)
    migrated = load_log(state / "execution_log.json")
    archive = state / migrated["migration"]["source_archive"]
    archive.write_text("{}", encoding="utf-8")
    with pytest.raises(StateError, match="archive hash mismatch"):
        status(config)


def test_migrated_log_requires_present_source_archive(tmp_path: Path) -> None:
    config, _, state, _ = compatibility_case(tmp_path)
    run_step(config)
    migrated = load_log(state / "execution_log.json")
    (state / migrated["migration"]["source_archive"]).unlink()
    with pytest.raises(StateError, match="archive is missing"):
        status(config)


def test_conflicting_historical_archive_and_non_file_state_paths_fail_closed(
    tmp_path: Path,
) -> None:
    config, _, state, historical_bytes = compatibility_case(tmp_path)
    source_content_hash = json.loads(historical_bytes)["content_sha256"]
    archive = state / f"execution_log.v0.2.0.{source_content_hash}.json"
    archive.write_bytes(b"conflict")
    with pytest.raises(StateError, match="archive conflicts"):
        run_step(config)

    archive.unlink()
    pending = state / ".paper_commit_pending.json"
    pending.mkdir()
    with pytest.raises(StateError, match="not a regular file"):
        status(config)
    pending.rmdir()

    (state / "execution_log.json").unlink()
    (state / "execution_log.json").mkdir()
    with pytest.raises(StateError, match="log path is not a regular file"):
        status(config)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are certified in Ubuntu CI")
def test_canonical_archive_symlink_fails_before_authority_commit(tmp_path: Path) -> None:
    config, _, state, historical_bytes = compatibility_case(tmp_path)
    source_content_hash = json.loads(historical_bytes)["content_sha256"]
    archive = state / f"execution_log.v0.2.0.{source_content_hash}.json"
    archive.symlink_to("execution_log.json")

    with pytest.raises(StateError, match="no-follow"):
        run_step(config)

    assert archive.is_symlink()
    assert (state / "execution_log.json").read_bytes() == historical_bytes


@pytest.mark.parametrize("attack", ["tamper", "replace"])
def test_archive_is_reverified_immediately_before_authority_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    if attack == "replace" and os.name == "nt":
        pytest.skip("Windows keeps the verified archive descriptor non-delete-share open")
    config, _, state, historical_bytes = compatibility_case(tmp_path)
    real_write = paper_engine.atomic_write_json
    attacked = False

    def attack_before_replace(path: Path, payload: object, **kwargs: object) -> None:
        nonlocal attacked
        callback = kwargs.get("before_replace")
        if path.name == "execution_log.json" and callable(callback):

            def attack_then_verify() -> None:
                nonlocal attacked
                archive = next(iter(state.glob("execution_log.v0.2.0.*.json")))
                if attack == "tamper":
                    archive.write_bytes(b"tampered-before-authority")
                else:
                    archive.unlink()
                    archive.write_bytes(historical_bytes)
                attacked = True
                callback()

            kwargs["before_replace"] = attack_then_verify
        real_write(path, payload, **kwargs)

    monkeypatch.setattr(paper_engine, "atomic_write_json", attack_before_replace)
    with pytest.raises(StateError, match="content changed|identity changed"):
        run_step(config)

    assert attacked
    assert (state / "execution_log.json").read_bytes() == historical_bytes


@pytest.mark.skipif(os.name == "nt", reason="Windows uses write-through moves, not directory fsync")
def test_archive_directory_is_synced_before_authority_replace_and_pending_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, _, _ = compatibility_case(tmp_path)
    events: list[str] = []
    real_link = paper_state.os.link
    real_replace = paper_state.os.replace
    real_sync = paper_state._sync_parent_directory
    real_clear = paper_engine.durable_unlink

    def observed_link(source: object, destination: object, **kwargs: object) -> None:
        events.append(f"link:{Path(destination).name}")
        real_link(source, destination, **kwargs)

    def observed_replace(source: object, destination: object) -> None:
        events.append(f"replace:{Path(destination).name}")
        real_replace(source, destination)

    def observed_sync(directory: Path) -> None:
        events.append("directory-fsync")
        real_sync(directory)

    def observed_clear(path: Path) -> None:
        events.append(f"clear:{path.name}")
        real_clear(path)

    monkeypatch.setattr(paper_state.os, "link", observed_link)
    monkeypatch.setattr(paper_state.os, "replace", observed_replace)
    monkeypatch.setattr(paper_state, "_sync_parent_directory", observed_sync)
    monkeypatch.setattr(paper_engine, "durable_unlink", observed_clear)

    run_step(config)

    archive_link = next(index for index, event in enumerate(events) if event.startswith("link:"))
    archive_sync = events.index("directory-fsync", archive_link + 1)
    pending_replace = events.index("replace:.paper_commit_pending.json")
    authority_replace = events.index("replace:execution_log.json")
    authority_sync = events.index("directory-fsync", authority_replace + 1)
    pending_clear = events.index("clear:.paper_commit_pending.json")
    assert archive_link < archive_sync < pending_replace
    assert pending_replace < authority_replace < authority_sync < pending_clear


@pytest.mark.skipif(os.name == "nt", reason="Windows uses write-through moves, not directory fsync")
def test_archive_directory_sync_failure_keeps_historical_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _, state, historical_bytes = compatibility_case(tmp_path)

    def fail_archive_sync(directory: Path) -> None:
        del directory
        raise OSError("injected archive directory fsync failure")

    monkeypatch.setattr(paper_state, "_sync_parent_directory", fail_archive_sync)
    with pytest.raises(OSError, match="archive directory fsync failure"):
        run_step(config)

    assert (state / "execution_log.json").read_bytes() == historical_bytes
    assert not (state / ".paper_commit_pending.json").exists()


@pytest.mark.parametrize(
    ("field", "version"),
    (("quant_paper_sim_version", "9.0.0"), ("quant_execution_version", "9.0.0")),
)
def test_unknown_authoritative_versions_fail_closed(field: str, version: str) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload[field] = version
    with pytest.raises(StateError, match="unsupported authoritative log version tuple"):
        validate_log(seal_log(payload))


def test_runtime_mismatch_and_compat_metadata_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical = json.loads(FIXTURE.read_text(encoding="utf-8"))
    monkeypatch.setattr(paper_state, "paper_sim_version", "9.0.0")
    with pytest.raises(StateError, match="quant-paper-sim runtime version mismatch"):
        validate_log(historical)
    monkeypatch.setattr(paper_state, "paper_sim_version", CURRENT_PAPER_SIM_VERSION)
    monkeypatch.setattr(paper_state, "execution_version", "9.0.0")
    with pytest.raises(StateError, match="quant-execution runtime version mismatch"):
        validate_log(historical)
    monkeypatch.setattr(paper_state, "execution_version", CURRENT_EXECUTION_VERSION)

    source_file_hash = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    migrated = migrate_historical_log(historical, source_file_sha256=source_file_hash)
    mutations = (
        (lambda value: value["migration"].pop("source_archive"), "metadata is malformed"),
        (
            lambda value: value["migration"].__setitem__("replay_profile", "wrong"),
            "metadata is not supported",
        ),
        (
            lambda value: value["migration"].__setitem__("source_file_sha256", "x" * 64),
            "hashes are malformed",
        ),
        (
            lambda value: value["migration"].__setitem__("source_archive", "wrong.json"),
            "archive name is not canonical",
        ),
    )
    for mutation, message in mutations:
        broken = deepcopy(migrated)
        mutation(broken)
        with pytest.raises(StateError, match=message):
            validate_log(seal_log(broken))


def test_historical_step_shape_and_migration_inputs_fail_closed() -> None:
    historical = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutations = []

    not_object = deepcopy(historical)
    not_object["steps"][0] = []
    mutations.append((not_object, "must be an object"))

    missing = deepcopy(historical)
    missing["steps"][0].pop("targets")
    mutations.append((missing, "missing fields"))

    duplicate = deepcopy(historical)
    duplicate["steps"].append(deepcopy(duplicate["steps"][0]))
    mutations.append((duplicate, "unique and strictly increasing"))

    checksum = deepcopy(historical)
    checksum["steps"][0]["input_sha256"] = "0" * 64
    mutations.append((checksum, "input checksum mismatch"))

    empty_targets = deepcopy(historical)
    empty_targets["steps"][0]["targets"] = []
    empty_targets["steps"][0]["input_sha256"] = sha256_payload(
        step_input_payload(empty_targets["steps"][0])
    )
    mutations.append((empty_targets, "non-empty array"))

    for payload, message in mutations:
        with pytest.raises(StateError, match=message):
            validate_log(seal_log(payload))

    with pytest.raises(StateError, match="file hash is malformed"):
        migrate_historical_log(historical, source_file_sha256="bad")
    current = new_log(initial_cash="100", execution_config={})
    with pytest.raises(StateError, match="only an exact"):
        migrate_historical_log(current, source_file_sha256="0" * 64)


def test_execution_artifact_schema_100_and_110_are_both_validated() -> None:
    historical = load_log(FIXTURE)
    compiled = compile_log(historical)
    for version in (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION):
        payloads = artifact_payloads(compiled, schema_version=version)
        assert len(payloads["orders"]) == 1
        assert len(payloads["fills"]) == 1
        assert len(payloads["ledger"]) == 3
        artifacts = compiled.runtime.engine.artifacts
        assert artifacts is not None
        result = execution_payload(artifacts.result, version=version)
        validate_json_record(RUN_RESULT_SCHEMA_ID, result, version)
    with pytest.raises(StateError, match="unsupported execution artifact schema"):
        artifact_payloads(compiled, schema_version="9.0.0")
