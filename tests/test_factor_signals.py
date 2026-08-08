from pathlib import Path

import yaml

from quant_paper_sim.engine import run_step


def test_paper_factor_smoke_config_and_step(tmp_path: Path) -> None:
    cfg_src = Path(__file__).resolve().parents[1] / "configs" / "paper_factor_smoke.yaml"
    assert cfg_src.is_file()
    cfg = yaml.safe_load(cfg_src.read_text(encoding="utf-8"))
    assert cfg["signals"]["factor_column"] == "momentum_20d"

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    signals = Path(__file__).parent / "fixtures" / "signals.yaml"
    regime = Path(__file__).parent / "fixtures" / "regime.json"
    config = cfg_dir / "run.yaml"
    config.write_text(
        f"""
state_dir: {state.as_posix()}
initial_capital: 100000
cash_reserve: 0.05
signals:
  source: yaml
  path: {signals.as_posix()}
  factor_column: momentum_20d
regime:
  path: {regime.as_posix()}
""".strip(),
        encoding="utf-8",
    )
    result = run_step(config)
    assert result.portfolio.nav > 0
