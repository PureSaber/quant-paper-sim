from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

from quant_paper_sim.models import SignalBundle, TargetPosition


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
    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"empty q5 csv: {path}")
    as_of = path.stem.replace("q5_candidates_", "")
    subset = df.head(top_n)
    weight = 1.0 / len(subset)
    targets = [
        TargetPosition(symbol=str(row["symbol"]), weight=weight, price=float(row["close"]))
        for _, row in subset.iterrows()
    ]
    return SignalBundle(
        as_of=as_of,
        targets=targets,
        regime_scale=regime_scale,
        cash_reserve=cash_reserve,
    )


def load_signals(cfg: dict, repo_root: Path) -> SignalBundle:
    sig = cfg.get("signals") or {}
    regime_cfg = cfg.get("regime") or {}
    regime_path = regime_cfg.get("path")
    regime_scale = load_regime_scale(
        Path(regime_path) if regime_path else None
    )
    if regime_cfg.get("override_scale") is not None:
        regime_scale = float(regime_cfg["override_scale"])

    cash_reserve = float(cfg.get("cash_reserve", 0.05))
    source = str(sig.get("source", "yaml"))
    path = Path(sig["path"])
    if not path.is_absolute():
        path = repo_root / path

    if source == "q5_csv":
        return load_q5_csv(path, int(sig.get("top_n", 10)), cash_reserve, regime_scale)
    return load_signal_yaml(path, cash_reserve, regime_scale)
