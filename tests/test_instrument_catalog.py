from __future__ import annotations

import json
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from quant_data_kit import AssetClass, dataclass_payload

from quant_paper_sim.engine import initialize, run_step, status
from quant_paper_sim.execution import instrument_spec, normalize_symbol
from quant_paper_sim.instrument_catalog import (
    BUNDLED_CATALOG_URI,
    DEFAULT_FIXTURE_QUERY_TIME,
    INSTRUMENT_RULE_PROFILE_VERSION,
    InstrumentCatalog,
    bundled_catalog_config,
    load_bundled_catalog,
    load_configured_catalog,
    load_instrument_catalog,
)
from quant_paper_sim.state import StateError, load_log, sha256_payload

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "src" / "quant_paper_sim" / "data" / "cn_a_core_fixture_v1.json"


def catalog_payload() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def write_catalog(tmp_path: Path, payload: dict, name: str = "catalog.json") -> InstrumentCatalog:
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return load_instrument_catalog(path)


def catalog_config(path: Path, catalog: InstrumentCatalog) -> dict[str, str]:
    return {
        "path": str(path),
        "logical_id": catalog.logical_id,
        "version": catalog.version,
        "certification_level": catalog.certification_level,
        "content_sha256": catalog.content_sha256,
    }


def normalized_catalog_hash_with_override(field: str, value: str) -> str:
    catalog = load_bundled_catalog()
    records = []
    for record in catalog.records:
        row = {
            "rule_profile_id": record.rule_profile_id,
            "instrument_spec": dataclass_payload(record.instrument),
            "symbol_mapping": dataclass_payload(record.mapping),
        }
        if record.mapping.provider_symbol == "000001":
            row["instrument_spec"][field] = value
        records.append(row)
    records.sort(
        key=lambda row: (
            row["symbol_mapping"]["provider_symbol"],
            row["symbol_mapping"]["effective_from"],
            row["symbol_mapping"]["available_at"],
            row["instrument_spec"]["instrument_id"],
        )
    )
    return sha256_payload(
        {
            "schema": "quant-paper-instrument-catalog/1.0.0",
            "logical_id": catalog.logical_id,
            "version": catalog.version,
            "certification_level": catalog.certification_level,
            "validity_semantics": catalog.validity_semantics,
            "records": records,
        }
    )


def state_file_bytes(state: Path) -> dict[str, bytes]:
    return {
        path.relative_to(state).as_posix(): path.read_bytes()
        for path in state.rglob("*")
        if path.is_file()
    }


def paper_case(
    tmp_path: Path,
    *,
    catalog: dict[str, str] | None = None,
    symbol: str = "000001",
    as_of: str = "2026-07-28",
) -> tuple[Path, Path, Path]:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    state = tmp_path / "state"
    signal = tmp_path / "signal.yaml"
    signal.write_text(
        yaml.safe_dump(
            {
                "as_of": as_of,
                "targets": [{"symbol": symbol, "weight": 1, "price": 10}],
            }
        ),
        encoding="utf-8",
    )
    config = config_dir / "paper.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(state),
                "initial_capital": 10_000,
                "commission_rate": 0.0003,
                "stamp_duty_rate": 0.0005,
                "stale_exit_price_policy": "last_known_signal_close",
                "instrument_rule_profile": INSTRUMENT_RULE_PROFILE_VERSION,
                "instrument_catalog": catalog or bundled_catalog_config(),
                "signals": {"source": "yaml", "path": str(signal)},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config, signal, state


def test_bundled_snapshot_certifies_only_explicit_fixtures_and_etf_product_type() -> None:
    catalog = load_bundled_catalog()
    applicability_start = datetime(2025, 1, 1, tzinfo=UTC)
    assert catalog.validity_semantics == "fixture_certification_applicability_not_listing_history"
    assert {record.instrument.effective_from for record in catalog.records} == {applicability_start}
    assert {record.mapping.effective_from for record in catalog.records} == {applicability_start}
    expected = {
        "600519",
        "000858",
        "601318",
        "000001",
        "000002",
        "600000",
        "688001",
        "510300",
        "159919",
    }
    resolved = {
        catalog.resolve(
            symbol,
            observation_time=DEFAULT_FIXTURE_QUERY_TIME,
            as_of_time=DEFAULT_FIXTURE_QUERY_TIME,
        ).mapping.provider_symbol
        for symbol in expected
    }
    assert resolved == expected
    stock = instrument_spec("600519", commission_rate="0.0003", stamp_duty_rate="0.0005")
    sse_etf = instrument_spec("510300.SH", commission_rate="0.0003", stamp_duty_rate="0.0005")
    szse_etf = instrument_spec("159919.SZ", commission_rate="0.0003", stamp_duty_rate="0.0005")
    assert stock.asset_class is AssetClass.EQUITY
    assert stock.product_type == "cn_a_share_paper"
    assert sse_etf.asset_class is szse_etf.asset_class is AssetClass.ETF
    assert sse_etf.product_type == szse_etf.product_type == "cn_etf_paper"
    assert sse_etf.price_tick.scale == szse_etf.price_tick.scale == 3


@pytest.mark.parametrize("symbol", ["159900", "000300", "600518"])
def test_range_classification_never_proves_identity_or_changes_log(
    tmp_path: Path, symbol: str
) -> None:
    config, signal, state = paper_case(tmp_path)
    initialize(config)
    authoritative_before = (state / "execution_log.json").read_bytes()
    signal.write_text(
        yaml.safe_dump(
            {
                "as_of": "2026-07-28",
                "targets": [{"symbol": symbol, "weight": 1, "price": 10}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StateError, match="not fixture-certified"):
        run_step(config)
    assert (state / "execution_log.json").read_bytes() == authoritative_before


def test_empty_exchange_suffix_is_rejected() -> None:
    with pytest.raises(StateError, match="suffix must not be empty"):
        normalize_symbol("600519.")


def test_fixture_certification_pit_boundaries(tmp_path: Path) -> None:
    catalog = load_bundled_catalog()
    at_start = datetime(2025, 1, 1, tzinfo=UTC)
    resolved = catalog.resolve("600519", observation_time=at_start, as_of_time=at_start)
    assert resolved.instrument.instrument_id == "CN.XSHG.600519"
    with pytest.raises(StateError, match="not fixture-certified"):
        catalog.resolve(
            "600519",
            observation_time=datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
            as_of_time=at_start,
        )
    with pytest.raises(StateError, match="not fixture-certified"):
        catalog.resolve(
            "600519",
            observation_time=at_start,
            as_of_time=datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC),
        )

    payload = catalog_payload()
    row = next(item for item in payload["records"] if item["provider_symbol"] == "600519")
    row["effective_to"] = "2026-08-01T00:00:00+00:00"
    row["superseded_at"] = "2026-09-01T00:00:00+00:00"
    bounded = write_catalog(tmp_path, payload)
    with pytest.raises(StateError, match="not fixture-certified"):
        bounded.resolve(
            "600519",
            observation_time=datetime(2026, 8, 1, tzinfo=UTC),
            as_of_time=datetime(2026, 8, 1, tzinfo=UTC),
        )
    with pytest.raises(StateError, match="not fixture-certified"):
        bounded.resolve(
            "600519",
            observation_time=datetime(2026, 7, 31, tzinfo=UTC),
            as_of_time=datetime(2026, 9, 1, tzinfo=UTC),
        )


def test_missing_damaged_duplicate_and_non_finite_catalogs_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(StateError, match="does not exist"):
        load_instrument_catalog(tmp_path / "missing.json")
    damaged = tmp_path / "damaged.json"
    damaged.write_text("{", encoding="utf-8")
    with pytest.raises(StateError, match="cannot be read"):
        load_instrument_catalog(damaged)
    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(StateError, match="cannot be read"):
        load_instrument_catalog(non_finite)

    duplicate = catalog_payload()
    duplicate["records"].append(deepcopy(duplicate["records"][0]))
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(StateError, match="ambiguous"):
        load_instrument_catalog(duplicate_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("venue", "XSHE", "venue"),
        ("asset_class", "etf", "asset_class"),
        ("product_type", "cn_etf_paper", "product_type"),
        ("quote_currency", "USD", "quote_currency"),
        ("settlement_currency", "USD", "settlement_currency"),
        ("calendar_id", "CRYPTO_24_7", "calendar_id"),
        ("price_tick", {"units": 1, "scale": 3}, "price_tick"),
        ("lot", 200, "lot"),
    ],
)
def test_catalog_metadata_must_match_rule_classification(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    payload = catalog_payload()
    row = next(item for item in payload["records"] if item["provider_symbol"] == "600519")
    row[field] = value
    path = tmp_path / f"wrong-{field}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StateError, match=message):
        load_instrument_catalog(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quote_currency", "USD"),
        ("settlement_currency", "USD"),
        ("calendar_id", "CRYPTO_24_7"),
    ],
)
def test_execution_metadata_conflict_fails_before_any_state_change(
    tmp_path: Path, field: str, value: str
) -> None:
    valid_path = tmp_path / "catalog-valid.json"
    shutil.copy(SNAPSHOT, valid_path)
    valid_catalog = load_instrument_catalog(valid_path)
    expected_original = {
        "quote_currency": "CNY",
        "settlement_currency": "CNY",
        "calendar_id": "CN-A-SHARE",
    }
    assert (
        normalized_catalog_hash_with_override(field, expected_original[field])
        == valid_catalog.content_sha256
    )
    config, _, state = paper_case(
        tmp_path,
        catalog=catalog_config(valid_path, valid_catalog),
    )
    initialize(config)
    before = state_file_bytes(state)
    before_log = load_log(state / "execution_log.json")
    before_artifacts = json.loads((state / "execution_artifacts.json").read_text(encoding="utf-8"))
    assert len(before_log["steps"]) == 0
    assert before_artifacts["orders"] == []
    assert before_artifacts["fills"] == []

    invalid_payload = catalog_payload()
    row = next(item for item in invalid_payload["records"] if item["provider_symbol"] == "000001")
    row[field] = value
    invalid_path = tmp_path / f"catalog-wrong-{field}.json"
    invalid_path.write_text(json.dumps(invalid_payload, indent=2), encoding="utf-8")
    invalid_hash = normalized_catalog_hash_with_override(field, value)
    assert invalid_hash != valid_catalog.content_sha256

    raw_config = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw_config["instrument_catalog"]["path"] = str(invalid_path)
    raw_config["instrument_catalog"]["content_sha256"] = invalid_hash
    config.write_text(yaml.safe_dump(raw_config, sort_keys=False), encoding="utf-8")

    fresh_config = tmp_path / f"fresh-{field}.yaml"
    fresh_state = tmp_path / f"fresh-state-{field}"
    fresh_raw = deepcopy(raw_config)
    fresh_raw["state_dir"] = str(fresh_state)
    fresh_config.write_text(yaml.safe_dump(fresh_raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(StateError, match=field):
        initialize(fresh_config)
    assert not fresh_state.exists()

    with pytest.raises(StateError, match=field):
        run_step(config)
    assert state_file_bytes(state) == before
    after_log = load_log(state / "execution_log.json")
    after_artifacts = json.loads((state / "execution_artifacts.json").read_text(encoding="utf-8"))
    assert len(after_log["steps"]) == len(before_log["steps"]) == 0
    assert after_artifacts["orders"] == before_artifacts["orders"] == []
    assert after_artifacts["fills"] == before_artifacts["fills"] == []
    assert after_artifacts["ledger"] == before_artifacts["ledger"]


def test_catalog_hash_path_semantics_and_replay_compatibility(tmp_path: Path) -> None:
    first_path = tmp_path / "catalog-a.json"
    shutil.copy(SNAPSHOT, first_path)
    first_catalog = load_instrument_catalog(first_path)
    config, _, state = paper_case(
        tmp_path,
        catalog=catalog_config(first_path, first_catalog),
    )
    run_step(config)
    authoritative_before = (state / "execution_log.json").read_bytes()
    log = load_log(state / "execution_log.json")
    identity = log["execution_config"]["instrument_catalog"]
    assert identity["content_sha256"] == first_catalog.content_sha256
    assert (
        identity["validity_semantics"] == "fixture_certification_applicability_not_listing_history"
    )
    assert str(first_path) not in json.dumps(log["execution_config"])

    second_path = tmp_path / "catalog-b.json"
    shutil.copy(SNAPSHOT, second_path)
    raw_config = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw_config["instrument_catalog"]["path"] = str(second_path)
    config.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
    status(config)
    assert (state / "execution_log.json").read_bytes() == authoritative_before

    raw_config["instrument_catalog"]["content_sha256"] = "0" * 64
    config.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
    with pytest.raises(StateError, match="hash mismatch"):
        status(config)
    assert (state / "execution_log.json").read_bytes() == authoritative_before

    changed_payload = catalog_payload()
    row = next(item for item in changed_payload["records"] if item["provider_symbol"] == "600519")
    row["available_at"] = "2025-02-01T00:00:00+00:00"
    changed_catalog = write_catalog(tmp_path, changed_payload, "catalog-c.json")
    raw_config["instrument_catalog"] = catalog_config(tmp_path / "catalog-c.json", changed_catalog)
    config.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
    with pytest.raises(StateError, match="configuration differs"):
        status(config)
    assert (state / "execution_log.json").read_bytes() == authoritative_before


def test_configured_catalog_requires_complete_explicit_identity(tmp_path: Path) -> None:
    config, _, _ = paper_case(tmp_path)
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    del cfg["instrument_catalog"]["content_sha256"]
    with pytest.raises(StateError, match="explicitly contain"):
        load_configured_catalog(cfg, config)
    cfg["instrument_catalog"] = {
        **bundled_catalog_config(),
        "path": "package://another/catalog.json",
    }
    with pytest.raises(StateError, match="unsupported instrument catalog package URI"):
        load_configured_catalog(cfg, config)


def test_three_complete_runs_have_identical_authoritative_and_execution_hashes(
    tmp_path: Path,
) -> None:
    config, _, state = paper_case(tmp_path)
    hashes: list[tuple[str, str, str, str, str]] = []
    for _ in range(3):
        initialize(config)
        run_step(config)
        log = load_log(state / "execution_log.json")
        result = log["steps"][-1]["cumulative_result"]
        hashes.append(
            (
                log["content_sha256"],
                result["order_sha256"],
                result["fill_sha256"],
                result["ledger_sha256"],
                result["result_sha256"],
            )
        )
    assert hashes[0] == hashes[1] == hashes[2]


def test_bundled_config_uses_logical_package_uri_not_absolute_path() -> None:
    config = bundled_catalog_config()
    assert config["path"] == BUNDLED_CATALOG_URI
    assert not Path(config["path"]).is_absolute()
