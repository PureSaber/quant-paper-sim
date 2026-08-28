from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import yaml

from quant_paper_sim.models import SignalBundle, TargetPosition


def _positive_top_n(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("signals.top_n must be a positive integer")
    return value


def _finite_numeric_column(
    frame: pd.DataFrame,
    column: str,
    *,
    source: str,
    positive: bool = False,
    non_negative: bool = False,
) -> pd.Series:
    numeric = pd.to_numeric(frame[column], errors="coerce")
    finite = numeric.map(lambda value: not pd.isna(value) and math.isfinite(float(value)))
    if not finite.all():
        raise ValueError(f"{source} column {column!r} must contain only finite numeric values")
    if positive and not (numeric > 0).all():
        raise ValueError(f"{source} column {column!r} must be positive")
    if non_negative and not (numeric >= 0).all():
        raise ValueError(f"{source} column {column!r} must be non-negative")
    return numeric


def _weights(frame: pd.DataFrame, *, source: str) -> pd.Series:
    if "weight" not in frame.columns:
        return pd.Series(1.0 / len(frame), index=frame.index)
    numeric = _finite_numeric_column(frame, "weight", source=source, non_negative=True)
    total = float(numeric.sum())
    if not math.isfinite(total) or total <= 0:
        raise ValueError(f"{source} weight total must be positive")
    return numeric / total


def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_regime_scale(path: Path | None) -> float:
    if path is None or not path.is_file():
        return 1.0
    data = json.loads(path.read_text(encoding="utf-8"))
    return float(data.get("position_scale", 1.0))


def load_signal_yaml(path: Path, cash_reserve: float, regime_scale: float) -> SignalBundle:
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    targets = []
    for row in raw.get("targets") or []:
        targets.append(
            TargetPosition(
                symbol=str(row["symbol"]),
                weight=float(row["weight"]),
                price=float(row["price"]),
            )
        )
    return SignalBundle(
        as_of=str(raw.get("as_of", "unknown")),
        targets=targets,
        regime_scale=regime_scale,
        cash_reserve=float(raw.get("cash_reserve", cash_reserve)),
    )


def load_q5_csv(path: Path, top_n: int, cash_reserve: float, regime_scale: float) -> SignalBundle:
    top_n = _positive_top_n(top_n)
    df = pd.read_csv(path, dtype={"symbol": str})
    missing = sorted({"symbol", "close"} - set(df.columns))
    if missing:
        raise ValueError(f"q5 csv is missing columns {missing}: {path}")
    if df.empty:
        raise ValueError(f"empty q5 csv: {path}")
    df["close"] = _finite_numeric_column(df, "close", source="q5 csv", positive=True)
    if "weight" in df.columns:
        df["weight"] = _finite_numeric_column(df, "weight", source="q5 csv", non_negative=True)
    as_of = path.stem.replace("q5_candidates_", "")
    subset = df.head(top_n).copy()
    weights = _weights(subset, source="q5 csv")
    targets = [
        TargetPosition(
            symbol=str(row["symbol"]),
            weight=float(weights.loc[index]),
            price=float(row["close"]),
        )
        for index, row in subset.iterrows()
    ]
    return SignalBundle(
        as_of=as_of,
        targets=targets,
        regime_scale=regime_scale,
        cash_reserve=cash_reserve,
    )


def load_factor_csv(
    path: Path,
    *,
    factor_column: str,
    price_column: str,
    top_n: int,
    cash_reserve: float,
    regime_scale: float,
) -> SignalBundle:
    top_n = _positive_top_n(top_n)
    df = pd.read_csv(path, dtype={"symbol": str})
    required = {"symbol", "date", factor_column, price_column}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"factor csv is missing columns {missing}: {path}")
    if df.empty:
        raise ValueError(f"empty factor csv: {path}")
    df[factor_column] = _finite_numeric_column(df, factor_column, source="factor csv")
    df[price_column] = _finite_numeric_column(df, price_column, source="factor csv", positive=True)
    if "weight" in df.columns:
        df["weight"] = _finite_numeric_column(df, "weight", source="factor csv", non_negative=True)
    as_of = str(df["date"].max())
    current = df.loc[df["date"].astype(str).eq(as_of)].copy()
    current = current.sort_values([factor_column, "symbol"], ascending=[False, True]).head(top_n)
    if current.empty:
        raise ValueError(f"factor csv has no rows for latest date {as_of}: {path}")
    weights = _weights(current, source="factor csv")
    targets = [
        TargetPosition(
            symbol=str(row["symbol"]),
            weight=float(weights.loc[index]),
            price=float(row[price_column]),
        )
        for index, row in current.iterrows()
    ]
    return SignalBundle(
        as_of=as_of,
        targets=targets,
        regime_scale=regime_scale,
        cash_reserve=cash_reserve,
    )


def resolve_data_path(raw: str, config_path: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    config_dir = config_path.parent
    for base in (config_dir, config_dir.parent):
        candidate = (base / path).resolve()
        if candidate.is_file():
            return candidate
    return (config_dir.parent / path).resolve()


def load_signals(cfg: dict, config_path: Path) -> SignalBundle:
    sig = cfg.get("signals") or {}
    regime_cfg = cfg.get("regime") or {}
    regime_path = regime_cfg.get("path")
    regime_scale = load_regime_scale(
        resolve_data_path(str(regime_path), config_path) if regime_path else None
    )
    if regime_cfg.get("override_scale") is not None:
        regime_scale = float(regime_cfg["override_scale"])

    cash_reserve = float(cfg.get("cash_reserve", 0.05))
    source = str(sig.get("source", "yaml"))
    path = resolve_data_path(str(sig["path"]), config_path)

    if source == "q5_csv":
        return load_q5_csv(path, sig.get("top_n", 10), cash_reserve, regime_scale)
    if source == "csv":
        factor_column = str(sig.get("factor_column", "")).strip()
        if not factor_column:
            raise ValueError("signals.factor_column is required for source: csv")
        return load_factor_csv(
            path,
            factor_column=factor_column,
            price_column=str(sig.get("price_column", "close")),
            top_n=sig.get("top_n", 10),
            cash_reserve=cash_reserve,
            regime_scale=regime_scale,
        )
    if source == "yaml":
        return load_signal_yaml(path, cash_reserve, regime_scale)
    raise ValueError(f"unsupported signals.source: {source!r}")
