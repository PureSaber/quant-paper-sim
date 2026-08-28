"""Versioned fixture-certified instrument catalog with strict PIT resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from quant_data_kit import (
    AssetClass,
    FixedPoint,
    InstrumentSpec,
    MarginMode,
    SymbolMapping,
    dataclass_payload,
    ensure_utc_datetime,
    validate_bitemporal_frame,
)
from quant_data_kit.exceptions import ValidationError

from quant_paper_sim.state import StateError, sha256_payload

UTC = timezone.utc
CATALOG_SCHEMA = "quant-paper-instrument-catalog/1.0.0"
CATALOG_CERTIFICATION = "fixture-certified"
INSTRUMENT_RULE_PROFILE_VERSION = "cn-exchange-product-rules-2026-08"
BUNDLED_CATALOG_LOGICAL_ID = "cn-a-core-paper-fixture"
BUNDLED_CATALOG_VERSION = "1.0.0"
BUNDLED_CATALOG_URI = "package://quant_paper_sim/data/cn_a_core_fixture_v1.json"
BUNDLED_CATALOG_SHA256 = "d37ed48c446e7d99a37c1989cf7d466ef30ab338c099401b299a237b89dec369"
DEFAULT_FIXTURE_QUERY_TIME = datetime(2026, 7, 28, 7, 0, tzinfo=UTC)

_CATALOG_KEYS = {
    "schema",
    "logical_id",
    "version",
    "certification_level",
    "validity_semantics",
    "records",
}
_RECORD_KEYS = {
    "instrument_id",
    "venue",
    "asset_class",
    "product_type",
    "native_symbol",
    "provider_symbol",
    "quote_currency",
    "settlement_currency",
    "calendar_id",
    "price_tick",
    "lot",
    "effective_from",
    "effective_to",
    "available_at",
    "superseded_at",
}
_CATALOG_CONFIG_KEYS = {
    "path",
    "logical_id",
    "version",
    "certification_level",
    "content_sha256",
}


@dataclass(frozen=True)
class InstrumentRuleProfile:
    """Classify exchange product rules without asserting instrument identity."""

    profile_id: str
    venue: str
    code_ranges: tuple[tuple[int, int], ...]
    asset_class: AssetClass
    product_type: str
    price_scale: int
    price_tick_units: int
    lot: int
    stamp_duty: bool

    def accepts(self, code: str) -> bool:
        value = int(code)
        return any(lower <= value <= upper for lower, upper in self.code_ranges)


INSTRUMENT_RULE_PROFILES = (
    InstrumentRuleProfile(
        profile_id="sse-main-a-v1",
        venue="XSHG",
        code_ranges=((600000, 601999), (603000, 603999), (605000, 605999)),
        asset_class=AssetClass.EQUITY,
        product_type="cn_a_share_paper",
        price_scale=2,
        price_tick_units=1,
        lot=100,
        stamp_duty=True,
    ),
    InstrumentRuleProfile(
        profile_id="sse-star-a-v1",
        venue="XSHG",
        code_ranges=((688000, 689999),),
        asset_class=AssetClass.EQUITY,
        product_type="cn_a_share_paper",
        price_scale=2,
        price_tick_units=1,
        lot=200,
        stamp_duty=True,
    ),
    InstrumentRuleProfile(
        profile_id="szse-main-a-v1",
        venue="XSHE",
        code_ranges=((1, 3999),),
        asset_class=AssetClass.EQUITY,
        product_type="cn_a_share_paper",
        price_scale=2,
        price_tick_units=1,
        lot=100,
        stamp_duty=True,
    ),
    InstrumentRuleProfile(
        profile_id="szse-chinext-a-v1",
        venue="XSHE",
        code_ranges=((300000, 301999),),
        asset_class=AssetClass.EQUITY,
        product_type="cn_a_share_paper",
        price_scale=2,
        price_tick_units=1,
        lot=100,
        stamp_duty=True,
    ),
    InstrumentRuleProfile(
        profile_id="sse-etf-rules-v1",
        venue="XSHG",
        code_ranges=((510000, 518999),),
        asset_class=AssetClass.ETF,
        product_type="cn_etf_paper",
        price_scale=3,
        price_tick_units=1,
        lot=100,
        stamp_duty=False,
    ),
    InstrumentRuleProfile(
        profile_id="szse-etf-rules-v1",
        venue="XSHE",
        code_ranges=((158000, 159999),),
        asset_class=AssetClass.ETF,
        product_type="cn_etf_paper",
        price_scale=3,
        price_tick_units=1,
        lot=100,
        stamp_duty=False,
    ),
)


@dataclass(frozen=True)
class CatalogRecord:
    instrument: InstrumentSpec
    mapping: SymbolMapping
    rule_profile_id: str


@dataclass(frozen=True)
class InstrumentCatalog:
    logical_id: str
    version: str
    certification_level: str
    validity_semantics: str
    content_sha256: str
    records: tuple[CatalogRecord, ...]

    @property
    def execution_identity(self) -> dict[str, str]:
        return {
            "schema": CATALOG_SCHEMA,
            "logical_id": self.logical_id,
            "version": self.version,
            "certification_level": self.certification_level,
            "validity_semantics": self.validity_semantics,
            "content_sha256": self.content_sha256,
        }

    def resolve(
        self,
        symbol: str,
        *,
        observation_time: datetime,
        as_of_time: datetime,
    ) -> CatalogRecord:
        code, rule = classify_symbol_rule(symbol)
        observation = _ensure_utc(observation_time, "observation_time")
        knowledge = _ensure_utc(as_of_time, "as_of_time")
        candidates = [
            record
            for record in self.records
            if record.mapping.source == "paper_signal"
            and record.mapping.provider_symbol == code
            and record.instrument.venue == rule.venue
            and _pit_valid(record.instrument, observation, knowledge)
            and _pit_valid(record.mapping, observation, knowledge)
        ]
        if not candidates:
            raise StateError(
                f"instrument {symbol!r} is not fixture-certified at observation={observation.isoformat()} "
                f"and as_of={knowledge.isoformat()} in catalog {self.logical_id}@{self.version}"
            )
        if len(candidates) != 1:
            raise StateError(f"instrument catalog resolution is ambiguous for {symbol!r}")
        record = candidates[0]
        _assert_rule_consistency(record.instrument, record.mapping, rule)
        return record


def classify_symbol_rule(symbol: str) -> tuple[str, InstrumentRuleProfile]:
    raw = str(symbol).strip().upper()
    if not raw:
        raise StateError("symbol is required")
    if raw.count(".") > 1:
        raise StateError(f"unsupported exchange symbol syntax: {symbol!r}")
    if "." in raw:
        code, suffix = raw.split(".", 1)
        if not suffix:
            raise StateError(f"exchange suffix must not be empty: {symbol!r}")
    else:
        code, suffix = raw, None
    if not code.isdigit() or len(code) != 6:
        raise StateError(f"unsupported exchange symbol syntax: {symbol!r}")
    suffix_venues = {
        "SH": "XSHG",
        "SS": "XSHG",
        "XSHG": "XSHG",
        "SZ": "XSHE",
        "XSHE": "XSHE",
    }
    if suffix is not None and suffix not in suffix_venues:
        raise StateError(f"unsupported exchange venue suffix: {suffix!r}")
    venue = suffix_venues.get(suffix)
    matches = [
        profile
        for profile in INSTRUMENT_RULE_PROFILES
        if (venue is None or profile.venue == venue) and profile.accepts(code)
    ]
    if len(matches) != 1:
        raise StateError(
            f"symbol is outside rule profile {INSTRUMENT_RULE_PROFILE_VERSION}: {symbol!r}"
        )
    return code, matches[0]


def bundled_catalog_config() -> dict[str, str]:
    return {
        "path": BUNDLED_CATALOG_URI,
        "logical_id": BUNDLED_CATALOG_LOGICAL_ID,
        "version": BUNDLED_CATALOG_VERSION,
        "certification_level": CATALOG_CERTIFICATION,
        "content_sha256": BUNDLED_CATALOG_SHA256,
    }


def load_bundled_catalog() -> InstrumentCatalog:
    return load_instrument_catalog(
        _bundled_catalog_path(),
        expected=bundled_catalog_config(),
    )


def load_configured_catalog(cfg: dict[str, Any], config_path: Path) -> InstrumentCatalog:
    raw = cfg.get("instrument_catalog")
    if not isinstance(raw, dict) or set(raw) != _CATALOG_CONFIG_KEYS:
        raise StateError(
            "instrument_catalog must explicitly contain path, logical_id, version, "
            "certification_level and content_sha256"
        )
    path = resolve_catalog_path(str(raw["path"]), config_path)
    return load_instrument_catalog(path, expected={key: str(value) for key, value in raw.items()})


def resolve_catalog_path(raw: str, config_path: Path) -> Path:
    if raw == BUNDLED_CATALOG_URI:
        return _bundled_catalog_path()
    if raw.startswith("package://"):
        raise StateError(f"unsupported instrument catalog package URI: {raw!r}")
    path = Path(raw)
    if path.is_absolute():
        return path
    for base in (config_path.resolve().parent, config_path.resolve().parent.parent):
        candidate = (base / path).resolve()
        if candidate.is_file():
            return candidate
    return (config_path.resolve().parent.parent / path).resolve()


def load_instrument_catalog(
    path: Path,
    *,
    expected: dict[str, str] | None = None,
) -> InstrumentCatalog:
    if not path.is_file():
        raise StateError(f"instrument catalog does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise StateError(f"instrument catalog cannot be read: {exc}") from exc
    catalog = _catalog_from_payload(payload)
    if expected is not None:
        _assert_expected_catalog(catalog, expected)
    return catalog


def _catalog_from_payload(payload: object) -> InstrumentCatalog:
    if not isinstance(payload, dict) or set(payload) != _CATALOG_KEYS:
        raise StateError(f"instrument catalog must contain exactly {sorted(_CATALOG_KEYS)}")
    if payload["schema"] != CATALOG_SCHEMA:
        raise StateError(f"unsupported instrument catalog schema: {payload['schema']!r}")
    logical_id = _required_text(payload["logical_id"], "logical_id")
    version = _required_text(payload["version"], "version")
    certification = _required_text(payload["certification_level"], "certification_level")
    if certification != CATALOG_CERTIFICATION:
        raise StateError("instrument catalog must be explicitly fixture-certified")
    validity_semantics = _required_text(payload["validity_semantics"], "validity_semantics")
    if validity_semantics != "fixture_certification_applicability_not_listing_history":
        raise StateError("instrument catalog validity semantics are not supported")
    rows = payload["records"]
    if not isinstance(rows, list) or not rows:
        raise StateError("instrument catalog records must be a non-empty array")
    records = tuple(
        _record_from_payload(row, logical_id=logical_id, version=version, index=index)
        for index, row in enumerate(rows)
    )
    _validate_catalog_intervals(records)
    normalized = {
        "schema": CATALOG_SCHEMA,
        "logical_id": logical_id,
        "version": version,
        "certification_level": certification,
        "validity_semantics": validity_semantics,
        "records": sorted(
            (_record_payload(record) for record in records),
            key=lambda row: (
                row["symbol_mapping"]["provider_symbol"],
                row["symbol_mapping"]["effective_from"],
                row["symbol_mapping"]["available_at"],
                row["instrument_spec"]["instrument_id"],
            ),
        ),
    }
    return InstrumentCatalog(
        logical_id=logical_id,
        version=version,
        certification_level=certification,
        validity_semantics=validity_semantics,
        content_sha256=sha256_payload(normalized),
        records=records,
    )


def _record_from_payload(
    row: object,
    *,
    logical_id: str,
    version: str,
    index: int,
) -> CatalogRecord:
    if not isinstance(row, dict) or set(row) != _RECORD_KEYS:
        raise StateError(f"instrument catalog records[{index}] has invalid fields")
    try:
        asset_class = AssetClass(_required_text(row["asset_class"], "asset_class"))
        tick = row["price_tick"]
        if (
            not isinstance(tick, dict)
            or set(tick) != {"units", "scale"}
            or isinstance(tick["units"], bool)
            or not isinstance(tick["units"], int)
            or isinstance(tick["scale"], bool)
            or not isinstance(tick["scale"], int)
        ):
            raise StateError("price_tick must contain integer units and scale")
        lot = row["lot"]
        if isinstance(lot, bool) or not isinstance(lot, int) or lot <= 0:
            raise StateError("lot must be a positive integer")
        effective_from = _parse_time(row["effective_from"], "effective_from")
        effective_to = _parse_optional_time(row["effective_to"], "effective_to")
        available_at = _parse_time(row["available_at"], "available_at")
        superseded_at = _parse_optional_time(row["superseded_at"], "superseded_at")
        instrument = InstrumentSpec(
            instrument_id=_required_text(row["instrument_id"], "instrument_id"),
            asset_class=asset_class,
            product_type=_required_text(row["product_type"], "product_type"),
            venue=_required_text(row["venue"], "venue"),
            native_symbol=_required_text(row["native_symbol"], "native_symbol"),
            base_currency=None,
            quote_currency=_required_text(row["quote_currency"], "quote_currency"),
            settlement_currency=_required_text(row["settlement_currency"], "settlement_currency"),
            price_tick=FixedPoint(tick["units"], tick["scale"]),
            quantity_step=FixedPoint(lot, 0),
            contract_multiplier=FixedPoint(1, 0),
            calendar_id=_required_text(row["calendar_id"], "calendar_id"),
            margin_mode=MarginMode.NONE,
            effective_from=effective_from,
            effective_to=effective_to,
            available_at=available_at,
            superseded_at=superseded_at,
            metadata={
                "certification_level": CATALOG_CERTIFICATION,
                "catalog_logical_id": logical_id,
                "catalog_version": version,
                "lot_size": str(lot),
                "price_scale": str(tick["scale"]),
                "market_data_mode": "paper_research_close_not_live",
            },
        )
        mapping = SymbolMapping(
            source="paper_signal",
            provider_symbol=_required_text(row["provider_symbol"], "provider_symbol"),
            instrument_id=instrument.instrument_id,
            effective_from=effective_from,
            effective_to=effective_to,
            available_at=available_at,
            superseded_at=superseded_at,
        )
    except (ValueError, ValidationError) as exc:
        raise StateError(f"invalid instrument catalog record {index}: {exc}") from exc
    _, rule = classify_symbol_rule(mapping.provider_symbol)
    _assert_rule_consistency(instrument, mapping, rule)
    return CatalogRecord(instrument=instrument, mapping=mapping, rule_profile_id=rule.profile_id)


def _assert_rule_consistency(
    instrument: InstrumentSpec,
    mapping: SymbolMapping,
    rule: InstrumentRuleProfile,
) -> None:
    expected_id = f"CN.{rule.venue}.{mapping.provider_symbol}"
    expected_native = f"{mapping.provider_symbol}.{rule.venue}"
    mismatches = []
    if instrument.instrument_id != mapping.instrument_id or instrument.instrument_id != expected_id:
        mismatches.append("instrument_id")
    if instrument.venue != rule.venue:
        mismatches.append("venue")
    if instrument.native_symbol != expected_native:
        mismatches.append("native_symbol")
    if instrument.asset_class is not rule.asset_class:
        mismatches.append("asset_class")
    if instrument.product_type != rule.product_type:
        mismatches.append("product_type")
    if instrument.price_tick != FixedPoint(rule.price_tick_units, rule.price_scale):
        mismatches.append("price_tick")
    if instrument.quantity_step != FixedPoint(rule.lot, 0):
        mismatches.append("lot")
    if mismatches:
        raise StateError(
            "instrument catalog metadata conflicts with code rule classification: "
            f"{sorted(mismatches)}"
        )


def _validate_catalog_intervals(records: tuple[CatalogRecord, ...]) -> None:
    try:
        validate_bitemporal_frame(
            pd.DataFrame(
                [
                    {
                        "instrument_id": record.instrument.instrument_id,
                        "effective_from": record.instrument.effective_from,
                        "effective_to": record.instrument.effective_to,
                        "available_at": record.instrument.available_at,
                        "superseded_at": record.instrument.superseded_at,
                    }
                    for record in records
                ]
            ),
            key_columns=("instrument_id",),
        )
        validate_bitemporal_frame(
            pd.DataFrame(
                [
                    {
                        "source": record.mapping.source,
                        "provider_symbol": record.mapping.provider_symbol,
                        "effective_from": record.mapping.effective_from,
                        "effective_to": record.mapping.effective_to,
                        "available_at": record.mapping.available_at,
                        "superseded_at": record.mapping.superseded_at,
                    }
                    for record in records
                ]
            ),
            key_columns=("source", "provider_symbol"),
        )
    except ValidationError as exc:
        raise StateError(f"instrument catalog has ambiguous or invalid PIT records: {exc}") from exc


def _record_payload(record: CatalogRecord) -> dict[str, object]:
    return {
        "rule_profile_id": record.rule_profile_id,
        "instrument_spec": dataclass_payload(record.instrument),
        "symbol_mapping": dataclass_payload(record.mapping),
    }


def _assert_expected_catalog(catalog: InstrumentCatalog, expected: dict[str, str]) -> None:
    if set(expected) != _CATALOG_CONFIG_KEYS:
        raise StateError("instrument catalog expected identity has invalid fields")
    actual = {
        "logical_id": catalog.logical_id,
        "version": catalog.version,
        "certification_level": catalog.certification_level,
        "content_sha256": catalog.content_sha256,
    }
    configured = {key: str(expected[key]) for key in actual}
    if configured != actual:
        raise StateError(
            f"instrument catalog identity or canonical content hash mismatch: "
            f"configured={configured}, actual={actual}"
        )


def _pit_valid(
    value: InstrumentSpec | SymbolMapping, observation: datetime, knowledge: datetime
) -> bool:
    return (
        value.effective_from <= observation
        and (value.effective_to is None or observation < value.effective_to)
        and value.available_at <= knowledge
        and (value.superseded_at is None or knowledge < value.superseded_at)
    )


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise StateError(f"{field} must be an ISO UTC datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError(f"{field} must be an ISO UTC datetime") from exc
    return _ensure_utc(parsed, field)


def _parse_optional_time(value: object, field: str) -> datetime | None:
    return None if value is None else _parse_time(value, field)


def _ensure_utc(value: datetime, field: str) -> datetime:
    try:
        return ensure_utc_datetime(value, field=field)
    except ValidationError as exc:
        raise StateError(str(exc)) from exc


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"{field} must be non-empty text")
    return value.strip()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _bundled_catalog_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "cn_a_core_fixture_v1.json"
