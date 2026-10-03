import json

import pytest
import yaml
from test_execution_state import paper_config, write_signal

from quant_paper_sim import engine
from quant_paper_sim.cli import main
from quant_paper_sim.state import StateError


def snapshot(root):
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("saved", [False, True])
def test_preflight_is_read_only_without_replay_or_lock(tmp_path, monkeypatch, capsys, saved):
    config, signal, state = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    if saved:
        engine.run_step(config)
    before = snapshot(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not execute, lock, migrate or write")

    for name in (
        "_compile",
        "compile_log",
        "_commit",
        "state_writer_lock",
        "_migrate_compatible_authority",
    ):
        monkeypatch.setattr(engine, name, forbidden)
    main(["preflight", "--config", str(config)])
    result = json.loads(capsys.readouterr().out)
    assert result["software_preflight"] == "pass" and result["read_only"] is True
    assert result["symbols"] == 1 and result["investable"] is False
    assert result["saved_state"]["same_step_already_saved"] is saved
    assert result["saved_state"]["authoritative_log_exists"] is saved
    assert snapshot(tmp_path) == before
    assert state.exists() is saved


@pytest.mark.parametrize(
    "bad",
    [
        "missing_signal",
        "bad_regime",
        "zero_price",
        "zero_weight",
        "negative_weight",
        "duplicate",
        "unlisted",
        "bad_date",
        "cash_reserve",
        "capital",
        "fee",
    ],
)
def test_preflight_rejects_bad_inputs_without_state(tmp_path, bad):
    config, signal, state = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    cfg = yaml.safe_load(config.read_text())
    data = yaml.safe_load(signal.read_text())
    if bad == "bad_regime":
        cfg["regime"] = {"path": str(tmp_path / "missing.json")}
    elif bad == "zero_price":
        data["targets"][0]["price"] = 0
    elif bad == "zero_weight":
        data["targets"][0]["weight"] = 0
    elif bad == "negative_weight":
        data["targets"][0]["weight"] = -1
    elif bad == "duplicate":
        data["targets"].append(data["targets"][0].copy())
    elif bad == "unlisted":
        data["targets"][0]["symbol"] = "000999"
    elif bad == "bad_date":
        data["as_of"] = "unknown"
    elif bad == "cash_reserve":
        data["cash_reserve"] = 1
    elif bad == "capital":
        cfg["initial_capital"] = 0
    elif bad == "fee":
        cfg["commission_rate"] = -1
    config.write_text(yaml.safe_dump(cfg))
    signal.write_text(yaml.safe_dump(data))
    if bad == "missing_signal":
        signal.unlink()
    before = snapshot(tmp_path)
    with pytest.raises((StateError, ValueError, OSError)):
        engine.preflight(config)
    assert snapshot(tmp_path) == before and not state.exists()


@pytest.mark.parametrize("bad", ["missing_log", "checksum", "conflict", "older", "changed_fee"])
def test_preflight_checks_saved_state_prerequisites_without_repair(tmp_path, bad):
    config, signal, state = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    engine.run_step(config)
    if bad == "missing_log":
        (state / "execution_log.json").unlink()
    elif bad == "checksum":
        payload = json.loads((state / "execution_log.json").read_text())
        payload["initial_cash"] = "2"
        (state / "execution_log.json").write_text(json.dumps(payload))
    elif bad in {"conflict", "older"}:
        write_signal(
            signal,
            as_of="2026-07-27" if bad == "older" else "2026-07-28",
            targets=[("000001", 1.0, 11.0)],
        )
    else:
        cfg = yaml.safe_load(config.read_text())
        cfg["commission_rate"] = 0.002
        config.write_text(yaml.safe_dump(cfg))
    before = snapshot(tmp_path)
    with pytest.raises(StateError):
        engine.preflight(config)
    assert snapshot(tmp_path) == before


def test_preflight_rejects_source_change_during_read(tmp_path, monkeypatch):
    from quant_paper_sim.readers import signals

    config, signal, state = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    original = signals.load_signals

    def changing(*args):
        result = original(*args)
        signal.write_text(signal.read_text() + "\n# source changed\n")
        return result

    monkeypatch.setattr(signals, "load_signals", changing)
    with pytest.raises(StateError, match="changed during preflight"):
        engine.preflight(config)
    assert not state.exists()


def test_preflight_regime_and_cli_failure(tmp_path, capsys):
    config, signal, state = paper_config(tmp_path)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    regime = tmp_path / "regime.json"
    regime.write_text('{"position_scale":0.8}')
    cfg = yaml.safe_load(config.read_text())
    cfg["regime"] = {"path": str(regime)}
    config.write_text(yaml.safe_dump(cfg))
    before = snapshot(tmp_path)
    assert engine.preflight(config)["software_preflight"] == "pass"
    assert snapshot(tmp_path) == before
    regime.unlink()
    with pytest.raises(SystemExit) as error:
        main(["preflight", "--config", str(config)])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == "" and "error:" in output.err
    assert not state.exists()
