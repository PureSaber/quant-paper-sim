# quant-paper-sim

`quant-paper-sim`v0.2 is a thin, deterministic CLI adapter over
[`quant-execution`](https://github.com/PureSaber/quant-execution) v0.2.0. It does not
maintain a second portfolio implementation and it has no live-order path.

## Commands

```bash
pip install -e ".[dev]"
quant-paper init --config configs/default.yaml
quant-paper step --config configs/default.yaml
quant-paper status --config configs/default.yaml
pytest --cov=quant_paper_sim --cov-branch --cov-fail-under=80 -q
```

The existing pipeline entry point remains supported without a preceding `init`:

```bash
quant-paper step --config configs/pipeline.yaml
```

On a completely new state directory, `step` records an explicit
`fresh_step_bootstrap` migration and opens the configured cash ledger. `init` always resets
the authoritative journal and all compatibility projections.

## Execution path

Every accepted step is replayed through one path:

```text
signal targets
  -> target/current quantity differences
  -> OrderIntent
  -> DeterministicRunEngine
  -> BarMatchingModel
  -> RuleBookRiskGate
  -> ExactAccountLedger
  -> compatibility projections
```

One rebalance uses strictly increasing UTC event times and explicit phases:

```text
mark -> submit-sell -> fill-sell -> submit-buy -> fill-buy
```

Sell fills and fees reach the ledger before buy intents are checked. Unfilled sale proceeds
are never treated as cash, and `INSUFFICIENT_CASH` is never relaxed. Same-day A-share sales
remain subject to the `quant-execution` T+1 rule.

Input prices are interpreted as completed paper-research close bars. Every generated event is
marked `paper_signal_close:not_live`; these values are not represented as real-time market
data. If a held symbol disappears from the next target set, the configured
`last_known_signal_close` exit policy carries its prior research close forward and records that
provenance explicitly.

## Authoritative state

`state/execution_log.json` is the only persisted execution truth. It contains:

- schema and exact `quant-execution` version;
- initial CNY cash and immutable execution configuration;
- normalized signal inputs, source file hashes and research-data semantics;
- deterministic event/fill evidence and cumulative order/fill/ledger/result hashes;
- a checksum over the complete journal.

`step` and `status` replay the complete journal into `ExactAccountLedger`; they never restore
cash or positions from `portfolio.json`.

The following files are compatibility projections only and are regenerated from replay:

- `portfolio.json`
- `nav.csv`
- `holdings.csv`
- `trades.json` (actual fills only)
- `execution_artifacts.json` (derived market events, orders, order events, fills, fees and ledger
  transactions)
- `projection_manifest.json`

Writes use same-directory temporary files, fsync and atomic replacement. A
`.paper_commit_pending.json` marker makes interrupted multi-file projection commits detectable;
when an older authoritative log still exists, the next valid command replays that log and repairs
the projections. If the log is absent, any pending marker, projection manifest, execution artifact,
or v2 marker in `portfolio.json` is treated as evidence of an interrupted/lost authoritative commit
and fails closed. Projection cash is never used to reconstruct the journal.

If no journal exists, a legacy cash-only `portfolio.json` can be migrated once with an explicit
`legacy_cash_only` record only when it has explicit positive cash, no holdings, and no v2 markers.
A legacy file containing holdings or zero/missing cash cannot be reconstructed without inventing
fills, cost basis, or principal and therefore fails closed with instructions to archive the old
state and run `init`.

## Signals

Supported `signals.source` values are:

- `yaml`: targets with `symbol`, `weight` and `price`;
- `q5_csv`: the existing Q5 candidate format with `symbol` and `close`;
- `csv`: factor rows with `symbol`, `date`, configured `factor_column`, and `close` (or a
  configured `price_column`). The latest date is selected, sorted by factor, and limited by
  `top_n`.

`top_n` must be a positive integer. Factor and price columns must be finite numerics and prices
must be positive. If a CSV has a `weight` column, every weight must be finite and non-negative and
the selected total must be positive; equal weighting is used only when that column is entirely
absent. Unknown sources, missing columns, non-UTC datetimes, invalid prices, negative weights and
duplicate instruments fail closed.

## Certified scope and limits

- Profile `cn-a-equity-core-etf-2026-08` certifies only the following conservative code ranges:
  SSE main A shares `600000-601999`, `603000-603999`, `605000-605999`; SSE STAR A shares
  `688000-689999`; SZSE main A shares `000001-003999`; SZSE ChiNext A shares
  `300000-301999`; SSE core ETF range `510000-518999`; and SZSE ETF ranges
  `158000-159999`.
- BSE, B shares, indices, convertible bonds, repos, LOFs, closed-end funds, REITs, warrants, and
  every code outside those profiles fail closed. This is an intentional certification boundary,
  not a claim that unsupported exchange products do not exist.
- A-share/ETF paper execution only; CNY base and settlement currency.
- Stable IDs use `CN.XSHG.<code>` or `CN.XSHE.<code>` instead of provider symbols.
- Board lots are 200 for `688`/`689` symbols and 100 otherwise.
- A-share prices use tick `CNY0.01`/scale 2. ETF prices use tick `CNY0.001`/scale 3; target,
  research Bar and limit-order precision all come from the same `InstrumentSpec`.
- Commission and sell-side stock stamp duty are explicit configuration values; ETF stamp duty
  is zero. Both configured rates must be finite and in `[0, 0.1]`. The current generic ETF paper
  rule remains conservatively T+1.
- No broker adapter, trading credentials or live order transmission.
- Signal-close bars assume full paper liquidity at the stated close and do not claim intraday,
  queue-position or real-time execution realism.

The tick rules are grounded in the official
[SSE Trading Rules (2026), section 3.3.11](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml),
[SSE ETF FAQ](https://www.sse.com.cn/assortment/fund/etf/question/c/c_20240118_5734754.shtml),
and [SZSE price-unit guidance](https://www.szse.cn/www/investor/index/update/t20200408_575791.html).
The SZSE ETF ranges follow the official
[code-range table](https://www.szse.cn/marketServices/technicalservice/doc/P020240510623040090566.pdf)
and [158-range expansion notice](https://www.szse.cn/marketServices/technicalservice/notice/t20230825_602945.html).

## Downstream compatibility

`quant-risk-monitor` can continue reading `holdings.csv` and `nav.csv`. `quant-pipeline` can
continue invoking `quant-paper step --config configs/pipeline.yaml`; config-relative and
repository-root-relative data paths retain their existing resolution order.
