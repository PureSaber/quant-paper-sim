# quant-paper-sim

Thin paper-trading CLI over the exact `quant-execution` v0.2.0 dependency.

## Invariants

- `state/execution_log.json` is authoritative; compatibility JSON/CSV files are projections.
- Cash, positions, NAV, orders, fills and fees must come from `quant-execution` replay.
- Never add a direct signal-to-holdings or signal-to-cash mutation path.
- Preserve `quant-paper init|step|status --config ...` and `configs/pipeline.yaml` path behavior.
- Generated signal-close bars must remain explicitly marked research-only and not-live.
- Sell fills must settle into the ledger before buy intents are risk-checked.
- Do not add broker credentials, live adapters or order-transmission code.

## Commands

```bash
pip install -e ".[dev]"
quant-paper init --config configs/default.yaml
quant-paper step --config configs/default.yaml
quant-paper status --config configs/default.yaml
ruff check src tests
pytest --cov=quant_paper_sim --cov-branch --cov-report=term-missing --cov-fail-under=80 -q
```

CI must remain green on Python3.10,3.11 and3.12.
