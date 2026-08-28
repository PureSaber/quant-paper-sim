"""Deterministic quant-execution adapter for A-share paper rebalancing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from quant_data_kit import (
    AssetClass,
    BarEvent,
    FixedPoint,
    InstrumentSpec,
    MarginMode,
    market_event_payload,
)
from quant_data_kit.exceptions import ValidationError
from quant_execution import (
    BarMatchingModel,
    DeterministicBroker,
    DeterministicRunEngine,
    ExactAccountLedger,
    OrderIntent,
    OrderType,
    RuleBookRiskGate,
    Side,
    TimeInForce,
    execution_payload,
)

from quant_paper_sim.state import StateError

UTC = timezone.utc
MONEY_SCALE = 8
ACCOUNT_ID = "paper-account"
STRATEGY_ID = "paper-rebalance-v2"
RUN_ID = "quant-paper-v2"
SEED = 42
INSTRUMENT_PROFILE_VERSION = "cn-a-equity-core-etf-2026-08"
MAX_PAPER_FEE_RATE = Decimal("0.1")
_INSTRUMENT_AVAILABLE_AT = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class CertifiedInstrumentProfile:
    """Conservative, auditable subset certified by this paper adapter."""

    profile_id: str
    venue: str
    code_ranges: tuple[tuple[int, int], ...]
    asset_class: AssetClass
    price_scale: int
    price_tick: str
    lot: int
    stamp_duty: bool

    def accepts(self, code: str) -> bool:
        value = int(code)
        return any(lower <= value <= upper for lower, upper in self.code_ranges)


# These deliberately conservative ranges are product profiles, not a general symbol guesser.
# Out-of-scope exchange products are rejected even when their first digit resembles a profile.
CERTIFIED_INSTRUMENT_PROFILES = (
    CertifiedInstrumentProfile(
        profile_id="sse-main-a-v1",
        venue="XSHG",
        code_ranges=((600000, 601999), (603000, 603999), (605000, 605999)),
        asset_class=AssetClass.EQUITY,
        price_scale=2,
        price_tick="0.01",
        lot=100,
        stamp_duty=True,
    ),
    CertifiedInstrumentProfile(
        profile_id="sse-star-a-v1",
        venue="XSHG",
        code_ranges=((688000, 689999),),
        asset_class=AssetClass.EQUITY,
        price_scale=2,
        price_tick="0.01",
        lot=200,
        stamp_duty=True,
    ),
    CertifiedInstrumentProfile(
        profile_id="szse-main-a-v1",
        venue="XSHE",
        code_ranges=((1, 3999),),
        asset_class=AssetClass.EQUITY,
        price_scale=2,
        price_tick="0.01",
        lot=100,
        stamp_duty=True,
    ),
    CertifiedInstrumentProfile(
        profile_id="szse-chinext-a-v1",
        venue="XSHE",
        code_ranges=((300000, 301999),),
        asset_class=AssetClass.EQUITY,
        price_scale=2,
        price_tick="0.01",
        lot=100,
        stamp_duty=True,
    ),
    CertifiedInstrumentProfile(
        profile_id="sse-core-etf-v1",
        venue="XSHG",
        code_ranges=((510000, 518999),),
        asset_class=AssetClass.ETF,
        price_scale=3,
        price_tick="0.001",
        lot=100,
        stamp_duty=False,
    ),
    CertifiedInstrumentProfile(
        profile_id="szse-core-etf-v1",
        venue="XSHE",
        code_ranges=((158000, 159999),),
        asset_class=AssetClass.ETF,
        price_scale=3,
        price_tick="0.001",
        lot=100,
        stamp_duty=False,
    ),
)


def decimal_value(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StateError(f"{field} must be a finite decimal") from exc
    if not number.is_finite():
        raise StateError(f"{field} must be a finite decimal")
    return number


def fixed(value: object, scale: int, field: str) -> FixedPoint:
    try:
        return FixedPoint.from_decimal(decimal_value(value, field), scale)
    except ValidationError as exc:
        raise StateError(f"{field} is not exact at scale {scale}: {value!r}") from exc


def validated_fee_rate(value: object, field: str) -> str:
    rate = decimal_value(value, field)
    if not Decimal(0) <= rate <= MAX_PAPER_FEE_RATE:
        raise StateError(f"{field} must be in [0, {MAX_PAPER_FEE_RATE}]")
    return format(rate, "f")


def parse_as_of(value: str) -> tuple[datetime, date]:
    raw = str(value).strip()
    if not raw:
        raise StateError("signal as_of is required")
    try:
        if len(raw) == 10:
            trading_day = date.fromisoformat(raw)
            return datetime.combine(trading_day, time(7, 0), tzinfo=UTC), trading_day
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateError(f"signal as_of must be an ISO date or UTC datetime: {raw!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise StateError("datetime signal as_of must be timezone-aware UTC")
    normalized = parsed.astimezone(UTC)
    return normalized, normalized.date()


def certified_instrument_profile(symbol: str) -> tuple[str, CertifiedInstrumentProfile]:
    raw = str(symbol).strip().upper()
    if not raw:
        raise StateError("symbol is required")
    parts = raw.split(".")
    code = parts[0]
    if not code.isdigit() or len(code) != 6:
        raise StateError(f"unsupported A-share symbol: {symbol!r}")
    suffix = parts[1] if len(parts) == 2 else ""
    if len(parts) > 2:
        raise StateError(f"unsupported A-share symbol: {symbol!r}")
    suffix_venues = {"SH": "XSHG", "SS": "XSHG", "XSHG": "XSHG", "SZ": "XSHE", "XSHE": "XSHE"}
    if suffix and suffix not in suffix_venues:
        raise StateError(f"unsupported A-share venue suffix: {suffix!r}")
    venue = suffix_venues.get(suffix)
    matches = [
        profile
        for profile in CERTIFIED_INSTRUMENT_PROFILES
        if (venue is None or profile.venue == venue) and profile.accepts(code)
    ]
    if len(matches) != 1:
        raise StateError(
            f"symbol is outside certified profile {INSTRUMENT_PROFILE_VERSION}: {symbol!r}"
        )
    return code, matches[0]


def normalize_symbol(symbol: str) -> tuple[str, str, str]:
    code, profile = certified_instrument_profile(symbol)
    venue = profile.venue
    return code, venue, f"CN.{venue}.{code}"


def lot_size(symbol: str) -> int:
    _, profile = certified_instrument_profile(symbol)
    return profile.lot


def instrument_spec(
    symbol: str,
    *,
    commission_rate: str,
    stamp_duty_rate: str,
) -> InstrumentSpec:
    commission_rate = validated_fee_rate(commission_rate, "commission_rate")
    stamp_duty_rate = validated_fee_rate(stamp_duty_rate, "stamp_duty_rate")
    code, profile = certified_instrument_profile(symbol)
    venue = profile.venue
    instrument_id = f"CN.{venue}.{code}"
    return InstrumentSpec(
        instrument_id=instrument_id,
        asset_class=profile.asset_class,
        product_type="cn_a_share_paper",
        venue=venue,
        native_symbol=f"{code}.{venue}",
        base_currency=None,
        quote_currency="CNY",
        settlement_currency="CNY",
        price_tick=fixed(profile.price_tick, profile.price_scale, "price_tick"),
        quantity_step=fixed(profile.lot, 0, "quantity_step"),
        contract_multiplier=fixed(1, 0, "contract_multiplier"),
        calendar_id="CN-A-SHARE",
        margin_mode=MarginMode.NONE,
        effective_from=_INSTRUMENT_AVAILABLE_AT,
        available_at=_INSTRUMENT_AVAILABLE_AT,
        metadata={
            "instrument_profile": INSTRUMENT_PROFILE_VERSION,
            "profile_id": profile.profile_id,
            "price_scale": str(profile.price_scale),
            "lot_size": str(profile.lot),
            "commission_rate": commission_rate,
            "stamp_duty_rate": stamp_duty_rate if profile.stamp_duty else "0",
            "market_data_mode": "paper_research_close_not_live",
        },
    )


class IntentStrategy:
    """Emit only precompiled intents at explicit submit-phase events."""

    def __init__(self, intents: dict[str, tuple[OrderIntent, ...]]) -> None:
        self.intents = intents

    def reset(self) -> None:
        return None

    def on_event(self, context, event):
        del context
        return self.intents.get(event.event_id, ())


@dataclass(frozen=True)
class ReplayRuntime:
    engine: DeterministicRunEngine
    snapshot: object


@dataclass(frozen=True)
class StepView:
    as_of: str
    snapshot: object
    fill_ids: tuple[str, ...]
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompiledReplay:
    runtime: ReplayRuntime
    steps: tuple[StepView, ...]
    instrument_symbols: dict[str, str]
    latest_prices: dict[str, Decimal]
    latest_price_sources: dict[str, str]


def _runtime(
    *,
    events: list[BarEvent],
    intents: dict[str, tuple[OrderIntent, ...]],
    registry: dict[str, InstrumentSpec],
    initial_cash: str,
) -> ReplayRuntime:
    ledger = ExactAccountLedger(
        account_id=ACCOUNT_ID,
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fixed(initial_cash, MONEY_SCALE, "initial_cash")},
        money_scale=MONEY_SCALE,
    )
    engine = DeterministicRunEngine(
        run_id=RUN_ID,
        account_id=ACCOUNT_ID,
        strategy_id=STRATEGY_ID,
        strategy=IntentStrategy(intents),
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger, money_scale=MONEY_SCALE),
        matching_model=BarMatchingModel(registry, participation_rate="1"),
        ledger=ledger,
    )
    engine.replay(events, SEED)
    return ReplayRuntime(engine=engine, snapshot=ledger.snapshot())


def _bar(
    *,
    instrument_id: str,
    price: Decimal,
    at: datetime,
    trading_day: date,
    event_id: str,
    sequence: int,
    phase: str,
    volume: int = 0,
    source_detail: str = "current_signal_close",
    price_scale: int,
) -> BarEvent:
    price_value = fixed(price, price_scale, f"{event_id}.price")
    return BarEvent(
        event_id=event_id,
        instrument_id=instrument_id,
        event_time=at,
        received_at=at,
        available_at=at,
        source=f"paper_signal_close:not_live:{source_detail}:{phase}",
        trading_day=trading_day,
        session_id=f"paper:{trading_day.isoformat()}",
        sequence=sequence,
        bar_start=at - timedelta(microseconds=1),
        bar_end=at,
        open_price=price_value,
        high_price=price_value,
        low_price=price_value,
        close_price=price_value,
        volume=fixed(volume, 0, f"{event_id}.volume"),
        is_complete=True,
    )


def _intent(
    *,
    step_hash: str,
    event: BarEvent,
    instrument_id: str,
    side: Side,
    quantity: int,
    price: Decimal,
    price_scale: int,
) -> OrderIntent:
    return OrderIntent(
        idempotency_key=f"paper:{step_hash}:{side.value}:{instrument_id}",
        account_id=ACCOUNT_ID,
        strategy_id=STRATEGY_ID,
        instrument_id=instrument_id,
        side=side,
        quantity=fixed(quantity, 0, "order.quantity"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=event.available_at,
        limit_price=fixed(price, price_scale, "order.limit_price"),
        reduce_only=False,
    )


def _desired_quantities(step: dict[str, Any], nav: Decimal) -> dict[str, int]:
    reserve = decimal_value(step["cash_reserve"], "cash_reserve")
    scale = decimal_value(step["regime_scale"], "regime_scale")
    if not Decimal(0) <= reserve < Decimal(1):
        raise StateError("cash_reserve must be in [0, 1)")
    if not Decimal(0) <= scale <= Decimal(1):
        raise StateError("regime_scale must be in [0, 1]")
    total = sum(
        (decimal_value(row["weight"], "target.weight") for row in step["targets"]), Decimal(0)
    )
    if total <= 0:
        raise StateError("target weights must sum to a positive value")
    investable = nav * (Decimal(1) - reserve) * scale
    desired: dict[str, int] = {}
    for row in step["targets"]:
        weight = decimal_value(row["weight"], "target.weight")
        if weight < 0:
            raise StateError("target weights must be non-negative")
        price = decimal_value(row["price"], "target.price")
        if price <= 0:
            raise StateError("target price must be positive")
        lot = int(row["lot_size"])
        raw = investable * weight / total / price
        lots = (raw / lot).to_integral_value(rounding=ROUND_FLOOR)
        desired[row["instrument_id"]] = int(lots) * lot
    return desired


def compile_log(log: dict[str, Any]) -> CompiledReplay:
    config = log["execution_config"]
    if config.get("instrument_profile") != INSTRUMENT_PROFILE_VERSION:
        raise StateError("authoritative log uses an unsupported instrument profile")
    commission = validated_fee_rate(config["commission_rate"], "commission_rate")
    stamp = validated_fee_rate(config["stamp_duty_rate"], "stamp_duty_rate")
    registry: dict[str, InstrumentSpec] = {}
    instrument_symbols: dict[str, str] = {}
    for step in log["steps"]:
        for row in step["targets"]:
            spec = instrument_spec(row["symbol"], commission_rate=commission, stamp_duty_rate=stamp)
            expected_scale = int(spec.metadata["price_scale"])
            expected_fixed = fixed(row["price"], expected_scale, "target.price")
            persisted_fixed = row.get("price_fixed")
            mapping_matches = (
                spec.instrument_id == row.get("instrument_id")
                and spec.native_symbol == row.get("native_symbol")
                and int(row.get("lot_size", -1)) == int(spec.metadata["lot_size"])
                and row.get("profile_id") == spec.metadata["profile_id"]
                and int(row.get("price_scale", -1)) == expected_scale
                and isinstance(persisted_fixed, dict)
                and persisted_fixed.get("units") == expected_fixed.units
                and persisted_fixed.get("scale") == expected_fixed.scale
            )
            if not mapping_matches:
                raise StateError(
                    "persisted target instrument mapping does not match certified rules"
                )
            registry[spec.instrument_id] = spec
            instrument_symbols[spec.instrument_id] = row["symbol"]

    events: list[BarEvent] = []
    intents: dict[str, tuple[OrderIntent, ...]] = {}
    latest_prices: dict[str, Decimal] = {}
    latest_price_sources: dict[str, str] = {}
    step_views: list[StepView] = []
    sequence = 0
    previous_time: datetime | None = None
    empty_runtime = _runtime(
        events=[], intents={}, registry=registry, initial_cash=log["initial_cash"]
    )
    current_runtime = empty_runtime

    for step_index, step in enumerate(log["steps"]):
        base_at, trading_day = parse_as_of(step["as_of"])
        if trading_day.isoformat() != step["trading_day"]:
            raise StateError("persisted trading_day does not match as_of")
        if previous_time is not None and base_at <= previous_time:
            raise StateError("step event time would not be strictly increasing")
        target_prices: dict[str, Decimal] = {}
        for row in step["targets"]:
            price = decimal_value(row["price"], "target.price")
            target_prices[row["instrument_id"]] = price
            latest_prices[row["instrument_id"]] = price
            latest_price_sources[row["instrument_id"]] = "current_signal_close"

        prior_positions = current_runtime.snapshot.positions
        for instrument_id in prior_positions:
            if instrument_id not in latest_prices:
                raise StateError(
                    f"missing any paper close price for held instrument {instrument_id}"
                )

        relevant = sorted(set(prior_positions) | set(target_prices))
        for instrument_id in relevant:
            latest_price_sources[instrument_id] = (
                "current_signal_close"
                if instrument_id in target_prices
                else "carried_forward_prior_signal_close"
            )
        step_event_ids: list[str] = []
        cursor = base_at
        for instrument_id in relevant:
            sequence += 1
            source_detail = (
                "current_signal_close"
                if instrument_id in target_prices
                else "carried_forward_prior_signal_close"
            )
            event = _bar(
                instrument_id=instrument_id,
                price=latest_prices[instrument_id],
                at=cursor,
                trading_day=trading_day,
                event_id=f"paper:{step_index}:mark:{instrument_id}",
                sequence=sequence,
                phase="mark",
                source_detail=source_detail,
                price_scale=int(registry[instrument_id].metadata["price_scale"]),
            )
            events.append(event)
            step_event_ids.append(event.event_id)
            cursor += timedelta(microseconds=1)
        current_runtime = _runtime(
            events=events, intents=intents, registry=registry, initial_cash=log["initial_cash"]
        )
        desired = _desired_quantities(step, current_runtime.snapshot.nav.to_decimal())
        positions = {
            instrument_id: value.to_decimal()
            for instrument_id, value in current_runtime.snapshot.positions.items()
        }
        sell_quantities = {
            instrument_id: int(positions.get(instrument_id, Decimal(0)))
            - desired.get(instrument_id, 0)
            for instrument_id in sorted(set(positions) | set(desired))
            if int(positions.get(instrument_id, Decimal(0))) > desired.get(instrument_id, 0)
        }

        anchor = relevant[0]
        sequence += 1
        submit_sell = _bar(
            instrument_id=anchor,
            price=latest_prices[anchor],
            at=cursor,
            trading_day=trading_day,
            event_id=f"paper:{step_index}:submit-sell",
            sequence=sequence,
            phase="submit-sell",
            source_detail=latest_price_sources[anchor],
            price_scale=int(registry[anchor].metadata["price_scale"]),
        )
        events.append(submit_sell)
        step_event_ids.append(submit_sell.event_id)
        intents[submit_sell.event_id] = tuple(
            _intent(
                step_hash=step["input_sha256"],
                event=submit_sell,
                instrument_id=instrument_id,
                side=Side.SELL,
                quantity=quantity,
                price=latest_prices[instrument_id],
                price_scale=int(registry[instrument_id].metadata["price_scale"]),
            )
            for instrument_id, quantity in sorted(sell_quantities.items())
        )
        cursor += timedelta(microseconds=1)
        for instrument_id, quantity in sorted(sell_quantities.items()):
            sequence += 1
            event = _bar(
                instrument_id=instrument_id,
                price=latest_prices[instrument_id],
                at=cursor,
                trading_day=trading_day,
                event_id=f"paper:{step_index}:fill-sell:{instrument_id}",
                sequence=sequence,
                phase="fill-sell",
                volume=quantity,
                source_detail=latest_price_sources[instrument_id],
                price_scale=int(registry[instrument_id].metadata["price_scale"]),
            )
            events.append(event)
            step_event_ids.append(event.event_id)
            cursor += timedelta(microseconds=1)

        after_sells = _runtime(
            events=events, intents=intents, registry=registry, initial_cash=log["initial_cash"]
        )
        after_sell_positions = {
            instrument_id: value.to_decimal()
            for instrument_id, value in after_sells.snapshot.positions.items()
        }
        buy_quantities = {
            instrument_id: desired_quantity
            - int(after_sell_positions.get(instrument_id, Decimal(0)))
            for instrument_id, desired_quantity in sorted(desired.items())
            if desired_quantity > int(after_sell_positions.get(instrument_id, Decimal(0)))
        }

        sequence += 1
        submit_buy = _bar(
            instrument_id=anchor,
            price=latest_prices[anchor],
            at=cursor,
            trading_day=trading_day,
            event_id=f"paper:{step_index}:submit-buy",
            sequence=sequence,
            phase="submit-buy",
            source_detail=latest_price_sources[anchor],
            price_scale=int(registry[anchor].metadata["price_scale"]),
        )
        events.append(submit_buy)
        step_event_ids.append(submit_buy.event_id)
        intents[submit_buy.event_id] = tuple(
            _intent(
                step_hash=step["input_sha256"],
                event=submit_buy,
                instrument_id=instrument_id,
                side=Side.BUY,
                quantity=quantity,
                price=latest_prices[instrument_id],
                price_scale=int(registry[instrument_id].metadata["price_scale"]),
            )
            for instrument_id, quantity in sorted(buy_quantities.items())
        )
        cursor += timedelta(microseconds=1)
        for instrument_id, quantity in sorted(buy_quantities.items()):
            sequence += 1
            event = _bar(
                instrument_id=instrument_id,
                price=latest_prices[instrument_id],
                at=cursor,
                trading_day=trading_day,
                event_id=f"paper:{step_index}:fill-buy:{instrument_id}",
                sequence=sequence,
                phase="fill-buy",
                volume=quantity,
                source_detail=latest_price_sources[instrument_id],
                price_scale=int(registry[instrument_id].metadata["price_scale"]),
            )
            events.append(event)
            step_event_ids.append(event.event_id)
            cursor += timedelta(microseconds=1)

        current_runtime = _runtime(
            events=events, intents=intents, registry=registry, initial_cash=log["initial_cash"]
        )
        artifacts = current_runtime.engine.artifacts
        if artifacts is None:
            raise StateError("quant-execution did not produce replay artifacts")
        event_id_set = set(step_event_ids)
        fill_ids = tuple(
            fill.fill_id
            for fill in artifacts.fills
            if any(
                market.event_id in event_id_set and market.available_at == fill.event_time
                for market in artifacts.market_events
            )
        )
        step_views.append(
            StepView(
                as_of=step["as_of"],
                snapshot=current_runtime.snapshot,
                fill_ids=fill_ids,
                event_ids=tuple(step_event_ids),
            )
        )
        previous_time = cursor - timedelta(microseconds=1)

    return CompiledReplay(
        runtime=current_runtime,
        steps=tuple(step_views),
        instrument_symbols=instrument_symbols,
        latest_prices=latest_prices,
        latest_price_sources=latest_price_sources,
    )


def artifact_payloads(compiled: CompiledReplay) -> dict[str, list[dict[str, Any]]]:
    artifacts = compiled.runtime.engine.artifacts
    if artifacts is None:
        return {
            "market_events": [],
            "orders": [],
            "order_events": [],
            "fills": [],
            "fees": [],
            "ledger": [],
        }
    return {
        "market_events": [market_event_payload(value) for value in artifacts.market_events],
        "orders": [execution_payload(value) for value in artifacts.orders],
        "order_events": [execution_payload(value) for value in artifacts.order_events],
        "fills": [execution_payload(value) for value in artifacts.fills],
        "fees": [execution_payload(value) for value in artifacts.fees],
        "ledger": [execution_payload(value) for value in artifacts.ledger_transactions],
    }
