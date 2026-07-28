# quant-paper-sim

Paper trading simulator.

## Commands

```bash
pip install -e ".[dev]"
quant-paper init --config configs/default.yaml
quant-paper step --config configs/default.yaml
quant-paper status --config configs/default.yaml
pytest -q
ruff check src tests
```
