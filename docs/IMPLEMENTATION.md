# Actual implementation and historical claims / 实现与历史描述

Reviewed for this export on 2026-11-13. The local code was read directly. No trading-account operation was performed. Historical reports are evidence of what was recorded, not proof of execution or profitability.

## Mechanism map

| Layer | Public code | Scope |
| --- | --- | --- |
| Main selection, adjacent Protection, cumulative ladder, flip/recovery Cases | `src/paired_ladder.py` | Original computational rules |
| Cash, fee/depth accounting, ordinary 0.998 exit, strict resolution checks | `src/paired_cash.py`, `src/paper_execution.py` | Original retained decision/accounting definitions; fresh temporary initialization |
| Phased Protection switches and nonnegative Main-net preview | `src/paired_cash_policy.py` | Full original policy |
| Paired-position Main-profit floor and uncovered-initial-flip exit | `paper_strategy/legacy.py`, `profit_floor.py` | Same retained logic; no live runner |
| Three-scenario Minimax versus direct SELL | `live_strategy/minimax_exit.py`, `planner.py`, `src/handover_minimax.py` | Offline computation, precision and fee reservation primitives |
| S2/H2 continuous account replay | `scripts/v4_continuous.py`, `v4_hypotheses.py` | One synthetic/research account threaded over chronological events |
| A1 and other round-7 switches | `scripts/v4_round7.py` | Original hook and wrapper behavior |
| Common replay parsing/accounting | `scripts/v3_backtest.py`, `v3_minimax_cases.py`, `v3_tier_search.py` | Required adapters retained; data download/freeze orchestration excluded |

Protection selection is based on adjacent normalized prices; it can defer an infeasible first Protection purchase while limiting uncovered Main entry. Case 1/2/3 repairs are distinct from case-0 ordinary allocation. Ordinary paired additions also preserve the paired Main profit floor. Preconditions and order minimums still apply; these checks do not guarantee realized profit.

Protection switching requires the new candidate probability at least 10% and a gap of at least 3 percentage points. The phased cash policy treats Main below 80%, 80–90% and at least 90% differently; the tail keeps old Protection rather than performing ordinary tail handover. Original-C Minimax checks and the later Minimax-vs-SELL wrapper are separate layers. A failed precondition is not itself an instruction to liquidate.

The comparison includes all event BUY costs with fees and prior net SELL/REDEEM proceeds. Liquidation uses legal current bid depth and fees, preserves unsold shares and their scenario payouts, and chooses SELL on a tie. Missing sell valuation waits. A computed choice can lead to persistent SELL-only treatment. The 2% figure is an audit reference in this comparison, not a separate veto or a slippage guarantee. The comparison covers three named outcomes; another temperature outcome can be worse.

## Claims checked against code

| Topic | Historical wording | Verified implementation | Release treatment |
| --- | --- | --- | --- |
| H2 | 14:00 plus explicit spread/depth and both legs | `make_gate('both_spread_late')` checks the local clock, then calls `_no_main_only`; no explicit quality predicate | Preserve behavior and test this gap; a fix must get a new experiment ID |
| A1 | Only ordinary low-value add-ons; Protection unaffected | `buy_hook` checks `not flags['in_first_entry']` and `budget < 2` with tolerance; no reason/case exclusion | Document that Protection and repair can be suppressed |
| Execution | True fees described as faithful live execution | `replay_one` selects `tf.true_executable_quote`, `true_cost_for_shares`, `true_executable_minimax`; realized-cost quantity gating differs from reservation gating | Keep both adapter implementations distinct; test a book where selected quantities differ |
| S2 | Two confirmed minutes | Quality streak uses distinct timestamps, same Main, spread and summed ask shares. A repeated timestamp does not increment; failed quality resets. Both-leg feasibility is checked at entry, not necessarily on every preceding streak frame | Call them decision ticks, not an unconditional two-minute guarantee |
| Frozen H2 | A complete frozen reproduction | The reviewed frozen directory contains two Python snapshots but imports engines/adapters outside that directory; no self-contained full dependency environment | Do not call the freeze a full reproducible package |
| Historical results | Positive sums interpreted as profit | Stored ledgers mix recorded “official”, assumed and window-end proxy release | Keep aggregates and hashes; all historical returns marked not rerun |

The H2 comment and preregistration claimed an extra quality test that the branch does not implement. A release note beside that comment points out the mismatch without adding the missing test. Some original adapter comments likewise describe intent more broadly than their code; this document and tests identify the actual distinction.

## Result interpretation

The 68 recorded daily windows span 2026-08-19 to 2026-10-25. V3 windows are 360 sampled minute observations per day; they are not full-day forecasts or tick-by-tick executions. V3 price proxies and narrow missing-price-envelope midpoints are not historical Gamma signals. The additional directly collected frame format uses Gamma prices and public books; the formats must not be treated as identical evidence.

Fees are applied at consumed levels with rate 0.05 and exponent 1 in these continuous replays. That is an explicit historical research assumption, not proof of every past market's fee schedule. The true-fee quantity selector can accept sizes a reserved-cost budget gate would reject. Per-level VWAP improves accounting but does not establish live order feasibility.

Decisions and fills use sampled frames with no actual queue position, latency or unseen-book simulation. Identical book content does not replenish already consumed depth. A one-minute execution sensitivity can mix the current signal with the following book; it is an assumption, not a measured fill. The PUBLIC_GAMMA_BOOK format does not support that V3-specific delay branch; it is not exposed as a public runner option.

`settle_event` adds a winning residual payout if given a winner, otherwise uses window-end liquidation valuation as a capital-release proxy. The historical `assumed_ok` condition does not enforce exclusion of assumed winners. The supported release runner instead rejects assumed label status and passes only explicitly evidenced official labels. Unknown remains unknown, although the inherited continuous replay still uses its hypothetical end-of-window capital release. It can overstate available capital before real settlement.

Stored counts of 54 “official” labels are not independently established in this release. HKO observations, a high market price or a bucket inferred by rounding cannot substitute for the exact market's final official payout evidence. There is no verified real-account P&L in this repository.

## Export changes and behavior checks

The export is a definition/file allowlist, not a copy of the original project. [Source manifest](../provenance/source_manifest.json) records source/export hashes and AST-identical retained definitions. Original Git history, account migrations, key stores, HTTP clients, service runners, Telegram and acquisition orchestration were excluded.

Infrastructure changes are explicit: fresh temporary accounts replace account migration; import-time output-directory creation and winner-file reads are removed; the runner injects explicit local sources and labels; the unused legacy config default is a generic 200-unit example; HKT is a fixed UTC+08 offset for contemporary research. Calendar helpers needed only by production runners were excluded. No ladder, gate, Minimax objective, cost formula or exit body was changed to match a report.

The source-initializer differential check runs the original fresh-account initialization in isolated temporary accounts and compares the full two-event ledgers for baseline/S2/H2/A1 with the release initializer. The retained decision and replay bodies are AST-compared separately. These checks support extraction fidelity for the exercised cases; they do not establish all-state equivalence, historical result reproduction or real exchange behavior.

中文要点：以实际代码为准，不为符合旧报告而补门槛；假定赢家不冒充官方结算；收益数字与版本、输入和假设绑定后才能解释；改变行为须另立版本。
