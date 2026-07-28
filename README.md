# quant-paper-sim

Paper trading simulator (模拟盘): signals → simulated holdings → NAV.

## Commands

```bash
pip install -e ".[dev]"
quant-paper init --config configs/default.yaml
quant-paper step --config configs/default.yaml
quant-paper status --config configs/default.yaml
pytest -q
```

## Stack

- Reads signals YAML or `q5_candidates.csv` from a-share-multifactor
- Optional `quant-regime` `position_scale`
- Writes `state/holdings.csv` and `state/nav.csv` for **quant-risk-monitor**

## Related

- [quant-regime](../quant-regime)
- [quant-risk-monitor](../quant-risk-monitor)
- [a-share-multifactor](../a-share-multifactor)
