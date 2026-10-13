import pytest
from test_execution_state import paper_config, write_signal

from quant_paper_sim.engine import initialize, run_step
from quant_paper_sim.state import StateError


def test_reinitialization_keeps_all_existing_account_bytes(tmp_path):
    config, signal, state = paper_config(tmp_path)
    initialize(config)
    write_signal(signal, as_of="2026-07-28", targets=[("000001", 1.0, 10.0)])
    run_step(config)
    before = {p.relative_to(state): p.read_bytes() for p in state.rglob("*") if p.is_file()}
    with pytest.raises(StateError, match="empty state directory"):
        initialize(config)
    assert before == {p.relative_to(state): p.read_bytes() for p in state.rglob("*") if p.is_file()}


@pytest.mark.parametrize("name", ["portfolio.json", "nav.csv", ".paper_commit_pending.json"])
def test_partial_or_legacy_state_is_not_erased(tmp_path, name):
    config, _, state = paper_config(tmp_path)
    state.mkdir()
    (state / name).write_bytes(b"existing possibly damaged evidence")
    with pytest.raises(StateError, match="empty state directory"):
        initialize(config)
    assert (state / name).read_bytes() == b"existing possibly damaged evidence"
    assert not (state / "execution_log.json").exists()
