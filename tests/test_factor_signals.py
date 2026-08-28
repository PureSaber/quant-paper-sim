from pathlib import Path

import yaml

from quant_paper_sim.engine import run_step
from quant_paper_sim.state import load_log


def test_paper_factor_smoke_config_and_step(tmp_path: Path) -> None:
    cfg_src = Path(__file__).resolve().parents[1] / "configs" / "paper_factor_smoke.yaml"
    assert cfg_src.is_file()
    cfg = yaml.safe_load(cfg_src.read_text(encoding="utf-8"))
    assert cfg["signals"]["factor_column"] == "momentum_20d"

    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    state = tmp_path / "state"
    signals = Path(__file__).parent / "fixtures" / "factor_signals.csv"
    regime = Path(__file__).parent / "fixtures" / "regime.json"
    cfg["state_dir"] = str(state)
    cfg["signals"]["path"] = str(signals)
    cfg["regime"]["path"] = str(regime)
    config = cfg_dir / "run.yaml"
    config.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    result = run_step(config)
    assert result.portfolio.nav > 0
    assert {holding.symbol for holding in result.portfolio.holdings} == {
        "000001",
        "000002",
        "600000",
    }
    log = load_log(state / "execution_log.json")
    assert {row["instrument_id"] for row in log["steps"][0]["targets"]} == {
        "CN.XSHE.000001",
        "CN.XSHE.000002",
        "CN.XSHG.600000",
    }
