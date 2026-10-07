# quant-paper-sim

`quant-paper-sim`v0.2.2 is a thin, deterministic CLI adapter over
[`quant-execution`](https://github.com/PureSaber/quant-execution) annotated tag`v0.5.1`
(peeled commit`15e4e5c9dbaf2fe9b438732b2e94db295d5ea58c`). It does not
maintain a second portfolio implementation and it has no live-order path.

## Commands

```bash
python -m pip install --no-deps -r requirements.lock
python -m pip check
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
quant-paper init --config configs/default.yaml
quant-paper step --config configs/default.yaml
quant-paper status --config configs/default.yaml
quant-paper preflight --config configs/default.yaml
python -m pytest --cov=quant_paper_sim --cov-branch --cov-report=json:coverage.json -q
python -m coverage report --fail-under=80
python tools/check_branch_coverage.py coverage.json --threshold 90 state execution engine
```

The existing pipeline entry point remains supported without a preceding `init`:

```bash
quant-paper step --config configs/pipeline.yaml
```

`preflight`输出`quant.paper-preflight/v1`JSON，复用实际步进的信号、费用、
历史时点证券身份、价格精度和资金约束校验，并检查已有登记日志的结构、配置、
归档及信号日期冲突。它不执行回放、不生成订单、不取得写锁、不创建状态目录，
也不迁移或刷新账户投影。缺少权威日志但残留v2投影时明确失败。
这与会重新回放并刷新投影的`status`不同。

预检通过仅说明所检查的输入和保存状态前置条件通过；已有成交证据的回放一致性、
未来成交结果及真实市场适用性不在此命令的认证范围。预检不锁定文件，`step`
仍重新读取并校验。输入缺失或非法时返回非零状态，不重新下载或修复源文件。

On a completely new state directory, `step` records an explicit
`fresh_step_bootstrap` migration and opens the configured cash ledger. `init` always resets
the authoritative journal and all compatibility projections.

An omitted regime source uses scale`1.0`. Once`regime.path`is configured, the file must
be readable and contain a finite`position_scale`in`[0, 1]`; missing files, missing fields,
booleans and invalid values abort the step without appending to the authoritative journal.
An explicit`regime.override_scale`must satisfy the same range and does not bypass validation
of a configured source file. Zero is a valid risk-off scale.

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

### 低频账本核验、独立备份与恢复

```powershell
quant-paper verify --config configs/paper.yaml
quant-paper backup --config configs/paper.yaml --out backups/account-20261006
quant-paper restore --config configs/recovered.yaml --backup backups/account-20261006
```

`verify`只读回放权威日志，核对成交、费用、事件及累计账本哈希，并将全部账户投影
与回放结果比较；它不创建目录、不取写锁、不迁移和改写文件。返回
`replay_verified`、`projection_status`、`mismatched_files`及`pending_commit`。
`needs_rebuild`说明投影缺失、损坏或存在未清理提交标记；权威日志有效时可使用既有
`status`重新生成投影。日志或历史归档损坏时明确失败，不从投影余额重建账户。

`backup`取账户写锁，在真实回放通过后创建全新且独立的目录，保存权威日志原始字节、
全部嵌套历史归档及逐文件SHA-256清单。它不会覆盖已有备份；最后写入清单作为完成
标记。备份只包含账户事实，不包含信号、市场输入、运行环境或配置；这些另行保存。

恢复配置的`state_dir`必须指向空目录，成交规则和证券目录必须与备份一致。
`restore`先核验全部备份、版本白名单和回放哈希，再安装原始权威字节并生成投影。
恢复不要求原信号文件仍存在。原状态、现有账户与旧认证资料不会被覆盖；也无需`init`。
权威日志安装后若投影写入中断，使用`status --config configs/recovered.yaml`继续重建；
已存在状态的目录不会接受第二次`restore`。

验收包括提交前后杀死独立进程、写锁自动释放、重复步进只记一次、规则不匹配、
伪造投影清单、损坏/缺失备份及嵌套归档的独立恢复。当前锁定
`quant-execution@v0.5.1`，仍为低频研究模拟，不增加纳秒或真实券商执行能力。


`state/execution_log.json` is the only persisted execution truth. It contains:

- schema and exact `quant-execution` version;
- initial CNY cash and immutable execution configuration;
- the fixture catalog logical ID, version, normalized content SHA-256 and validity semantics;
- normalized signal inputs, source file hashes and research-data semantics;
- deterministic event/fill evidence and cumulative order/fill/ledger/result hashes;
- a checksum over the complete journal.

`step` and `status` replay the complete journal into `ExactAccountLedger`; they never restore
cash or positions from `portfolio.json`.

### Exact version compatibility

State compatibility is an allowlist, not a broad version fallback:

- New state is exactly`quant-paper-sim 0.2.2 + quant-execution 0.5.1`. It uses the current
  causal opening-time policy and records both versions in the authoritative log.
- Previous state is exactly`quant-paper-sim 0.2.1 + quant-execution 0.4.1`. Native0.2.1
  journals keep the causal opening-time policy. A0.2.1 journal that already migrated from0.2.0
  keeps the frozen historical profile and its existing source-archive chain.
- Historical state is exactly`quant-paper-sim 0.2.0 + quant-execution 0.2.0`. It is replayed
  by the same0.5.1 engine and`ExactAccountLedger`, with only the historical1970-01-01 UTC
  opening-time policy restored. This is required because0.3.0 made ledger opening causal;
  without the explicit policy, order and fill hashes remain equal but ledger and result hashes
  change.
- Any other schema/package-version tuple fails closed. A migrated log must contain the exact
  migration kind, replay profile, source versions, canonical archive name and two SHA-256s.

`status` treats both0.2.0 and0.2.1 journals as read-only: it verifies all persisted event, fill,
order, ledger and result hashes, regenerates projections, and never rewrites authoritative bytes.
On the first`step`, the immediate prior journal is copied byte-for-byte to
`execution_log.v<source-paper-version>.<content_sha256>.json` before a0.5.1 compatibility journal
is committed. The new journal anchors that archive's content and file hashes. If the0.2.1 source
already points to a0.2.0 archive, the complete nested chain is revalidated before every replay.
Future reads require every archive to remain present and unchanged. An interrupted migration leaves the old journal authoritative;
the next replay repairs projections from that journal, never from projection cash or holdings.

Execution artifact JSON supports the frozen`1.0.0`and current`1.1.0`schemas through
`quant-execution`validators. New projections declare and write`1.1.0`; compatibility tests
validate orders, order events, fills, fees, ledger transactions and run results against both
versions. The authoritative paper journal remains`quant-paper-execution-log/1.0.0`because its
shape did not change; package-version tuples and migration metadata select the exact replay policy.

The following files are compatibility projections only and are regenerated from replay:

- `portfolio.json`
- `nav.csv`
- `holdings.csv`
- `trades.json` (actual fills only)
- `execution_artifacts.json` (derived market events, orders, order events, fills, fees and ledger
  transactions)
- `projection_manifest.json`

State writers are serialized by a no-follow lock. Authority, archive and pending reads use
`lstat` plus a held no-follow regular-file descriptor; symlinks and detectable Windows reparse
points fail closed. The historical archive is installed exclusively, kept open through the
migration, and its path identity and exact bytes are checked again immediately before authority
replacement.

Writes use same-directory temporary files, file fsync and atomic replacement. On POSIX, every
archive install, replacement and pending-marker removal is followed by parent-directory fsync.
Windows cannot provide POSIX directory fsync through Python; it therefore uses
`MoveFileExW(..., MOVEFILE_WRITE_THROUGH)` for installs/replacements and durably renames the
active pending marker to a harmless tombstone before best-effort tombstone cleanup. The code does
not treat an unsupported Windows directory fsync as success. A
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

- `instrument_rule_profile: cn-exchange-product-rules-2026-08` only classifies exchange product
  rules. A matching code range never proves that a symbol exists, is listed, or is tradable.
- Execution identity comes from the explicitly configured, versioned PIT catalog
  `cn-a-core-paper-fixture@1.0.0`. Its fixture-certified rows are exactly `600519`, `000858`,
  `601318`, `000001`, `000002`, `600000`, `688001`, `510300` and `159919`; any unlisted symbol
  fails closed even if its code matches a rule range. For example, `159900`, `000300` and
  `600518` are rejected.
- The snapshot's `effective_from=2025-01-01T00:00:00Z` means "this fixture certification is
  applicable from this time." It is deliberately not an exchange listing date and does not
  describe the historical listing status of `688001`, either ETF, or any other row.
- The catalog is a deterministic research fixture, not a complete A-share security master,
  listing-history source, legal-data entitlement, or market-data-certified dataset. M4 must
  replace it with an authorized, versioned `quant-data-kit` PIT instrument directory before
  broader symbols or real historical identity claims can be certified.
- At each signal time, both the business-effective interval and knowledge interval
  (`available_at`/`superseded_at`) must select exactly one row. The catalog's canonical content
  hash is pinned in configuration and the authoritative log; a missing, ambiguous, modified,
  future, expired or superseded row fails closed. Catalog file paths are local lookup details and
  are not part of the stable execution hash.
- BSE, B shares, indices, convertible bonds, repos, LOFs, closed-end funds, REITs and warrants
  remain out of scope. A code-range rule is only a metadata consistency check after catalog
  resolution, not an allowlist by itself.
- A-share/ETF paper execution only; CNY base and settlement currency.
- Stable IDs use `CN.XSHG.<code>` or `CN.XSHE.<code>` instead of provider symbols.
- Board lots are 200 for `688`/`689` symbols and 100 otherwise.
- A-share prices use tick `CNY0.01`/scale 2. ETF prices use tick `CNY0.001`/scale 3; target,
  research Bar and limit-order precision all come from the same `InstrumentSpec`.
- Stocks use `product_type=cn_a_share_paper`; ETFs use `product_type=cn_etf_paper`.
- Every certified A-share/ETF rule requires quote currency `CNY`, settlement currency `CNY` and
  calendar `CN-A-SHARE`; external fixture catalogs with conflicting execution metadata fail
  before authoritative state is created or changed.
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
The classification-only SZSE ETF ranges follow the official
[code-range table](https://www.szse.cn/marketServices/technicalservice/doc/P020240510623040090566.pdf)
and [158-range expansion notice](https://www.szse.cn/marketServices/technicalservice/notice/t20230825_602945.html).

## Downstream compatibility

`quant-risk-monitor` can continue reading `holdings.csv` and `nav.csv`. `quant-pipeline` can
continue invoking `quant-paper step --config configs/pipeline.yaml`; config-relative and
repository-root-relative data paths retain their existing resolution order.

## Reproducible lock and rollback

`requirements.lock`is generated under Python3.10 and includes runtime, dev and editable-build
dependencies. Rebuild it only from`pyproject.toml`and`requirements-constraints.txt`:

```bash
python3.10 -m pip install pip-tools==7.6.1
python3.10 -m piptools compile --extra dev --build-deps-for editable \
  --allow-unsafe --strip-extras --resolver backtracking \
  --index-url https://pypi.org/simple \
  --constraint requirements-constraints.txt \
  --output-file requirements.lock pyproject.toml
```

Rollback source with`git revert <merge-commit>`and rerun the locked three-version CI; never move
or rebuild old tags. A state already migrated to0.2.2 is not readable by0.2.1. For a state-level
rollback, stop the process, preserve the migrated directory, copy the hash-verified immediate
prior archive into a new isolated state directory as`execution_log.json`, and run the matching
prior release's`status`to verify it before any further step. Do not edit any authoritative journal
or archive in place.
