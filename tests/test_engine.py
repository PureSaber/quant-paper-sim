from __future__ import annotations

from pathlib import Path

from quant_paper_sim.engine import rebalance, run_step
from quant_paper_sim.models import PortfolioState, SignalBundle, TargetPosition
from quant_paper_sim.readers.signals import load_q5_csv, load_regime_scale, load_signal_yaml


def test_regime_scale():
    path = Path(__file__).parent / "fixtures" / "regime.json"
    assert load_regime_scale(path) == 0.8
    assert load_regime_scale(None) == 1.0


def test_rebalance_respects_lot_and_scale():
    portfolio = PortfolioState(as_of="", cash=100_000, initial_capital=100_000)
    signals = SignalBundle(
        as_of="2026-07-28",
        targets=[TargetPosition("000001", 1.0, 12.0)],
        regime_scale=0.5,
        cash_reserve=0.05,
    )
    result = rebalance(portfolio, signals, commission_rate=0.0)
    assert result.portfolio.nav <= 100_000
    assert len(result.portfolio.holdings) == 1
    assert result.portfolio.holdings[0].shares % 100 == 0
    assert result.portfolio.cash > 0


def test_q5_csv_loader():
    path = Path(__file__).parent / "fixtures" / "q5_candidates.csv"
    bundle = load_q5_csv(path, top_n=3, cash_reserve=0.05, regime_scale=1.0)
    assert len(bundle.targets) == 3
    assert abs(sum(t.weight for t in bundle.targets) - 1.0) < 1e-9


def test_step_smoke(tmp_path: Path):
    cfg = tmp_path / "configs"
    cfg.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    signals = Path(__file__).parent / "fixtures" / "signals.yaml"
    regime = Path(__file__).parent / "fixtures" / "regime.json"
    config = cfg / "run.yaml"
    config.write_text(
        f"""
state_dir: {state.as_posix()}
initial_capital: 100000
cash_reserve: 0.05
signals:
  source: yaml
  path: {signals.as_posix()}
regime:
  path: {regime.as_posix()}
""".strip(),
        encoding="utf-8",
    )
    result = run_step(config)
    assert result.portfolio.nav > 0
    assert (state / "portfolio.json").is_file()
    assert (state / "holdings.csv").is_file()


def test_signal_yaml_loader():
    path = Path(__file__).parent / "fixtures" / "signals.yaml"
    bundle = load_signal_yaml(path, cash_reserve=0.05, regime_scale=0.8)
    assert bundle.as_of == "2026-07-28"
    assert len(bundle.targets) == 4
