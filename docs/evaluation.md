# Factor evaluation semantics

The main daily workflow evaluates factors under a declared local research convention. It is a software-validation and research tool, not a complete trading simulator. See `paper_alpha/evaluation.py`, `paper_alpha/worker.py`, and `paper_alpha/server/candidate_series.py` for the implemented contract.

## Inputs and expression checks

The daily CSV and metadata declare the universe, calendar, OHLCV fields, units, adjustment policy, field availability, and file checksum. Validation requires the expected grid and exact panel index/column alignment. A missing field or inconsistent axis is an error; the engine does not silently substitute a field or realign samples.

Expressions are parsed through a restricted Python AST. Allowed fields, operators, argument counts, windows, and complexity are checked before execution. Arbitrary Python code is not accepted. Rolling and cross-sectional calculations use the project's declared operator semantics, including diagnostics for unsupported or undefined cases.

## Validation interval and timing

The development flow evaluates the declared **validation** interval. Earlier train rows can supply trailing lookback. The worker truncates expression input at `validation.end`; future test rows do not enter factor calculation. The main evaluation rejects direct test evaluation.

| Event | Timing |
| --- | --- |
| Signal observation | After session `t` closes |
| Entry reference | Session `t+1` open |
| Exit reference | Session `t+2` open |
| Forward-return label | `open[t+2] / open[t+1] - 1` |
| Split boundary | Signal, entry, and exit must all be inside the selected interval |

Tail dates whose entry or exit crosses the interval are removed from evaluation and counted. The implementation does not borrow labels from the future test interval to fill those dates.

These controls apply to the declared data, operators, windows, and execution path. They do not prove that every external factor or real dataset has correct point-in-time semantics. Reading or storing a complete public fixture, including its declared test bytes, is not a blind-holdout guarantee.

## Weights and metrics

For finite factor observations in a valid cross-section, the evaluator computes average signal ranks, subtracts their mean, and normalizes by the sum of absolute centered ranks. The resulting local portfolio convention has gross exposure 1 and net exposure 0. The `min_assets` requirement and available forward returns constrain which days can be evaluated.

| Output | Meaning |
| --- | --- |
| Daily Rank IC | Pearson correlation of average cross-sectional factor and forward-return ranks |
| Mean Rank IC | Arithmetic mean over days with defined, nonconstant forward-return ranks |
| Daily gross return | Weighted forward-return label under the local rank-weight convention |
| Mean / cumulative gross return | Mean and arithmetic sum over evaluated days |
| Coverage | Available finite observations and the declared denominator |
| Valid, skipped, and boundary counts | Explicit treatment of insufficient assets, constant cross-sections, missing values, and interval edges |

An undefined metric remains null. A skipped date does not become a fabricated zero-return day. Daily result files retain the supporting asset detail and counts.

Charts show saved daily gross returns, their arithmetic cumulative sum, Rank IC, and coverage. They bind the run, revision, attempt, candidate, and result digest. They preserve gaps and boundary points; they do not recompute historical experiments.

## What these results do not establish

The main daily evaluator excludes fees, slippage, borrowing, fills, tradability, liquidity, and capacity. Arithmetic cumulative gross return is not compounded NAV or WorldQuant BRAIN PnL. The daily workflow does not establish a Sharpe ratio, maximum drawdown, automatic investment rating, or replication of the paper's trading performance.

The public example is synthetic. The Alpha101 development fixture has 12 synthetic assets across 400 synthetic weekday sessions, not an exchange-certified calendar or real equity prices. It is useful for execution, attribution, and failure-path checks; positive or negative sample results are not a financial achievement.

Alpha101 is also a limited formula-template case. It does not show support for all 101 factors or arbitrary BRAIN expressions. A candidate requiring `vwap` remains blocked when that field is missing; `close` is not an authorized substitute.

The code also contains independent monthly MOM-only and real-daily admission tooling. The former uses precomputed industry portfolios and its own method, not this daily DSL. The latter includes source/field/authority diagnostics; it does not imply that certified point-in-time equity data or final-period research is available in the delivered main workflow. Required external source materials are not preloaded in this public export.

## Formula engineering and scientific unknowns

A paper can specify a calculable formula without explaining its economic mechanism. The explicit formula-engineering policy preserves that mechanism as null with unresolved attribution instead of inventing an explanation. The original source questions stay recorded, and each receives an explicit disposition.

This policy still requires the source calculation, mathematical signal direction, data meaning, execution assumptions, and component-level support. Missing computational information remains a blocker. Exact source spans and the finite operator-definition check establish mechanical citation and definition coverage, not scientific validity or semantic entailment.

The optional model proposes structured material and candidate definitions. Deterministic tools validate and calculate them. The limited local model acceptance used one previously seen Alpha101 case and synthetic data; it is scoped engineering evidence rather than an independent benchmark of model understanding.

## Verification boundaries

Representative tests cover timing, grid/checksum validation, missing and constant data, exact alignment, and expression rules. Independent reference checks compare supported numerical cases. Backup tests verify artifact and history preservation.

Those checks prove scoped software behavior. They do not measure profitability, general model quality, real-user time savings, or monetary efficiency. Deterministic corrections resolve supported representation errors and selected-source definition-citation gaps. They preserve scientific content and historical versions; they are not financial optimization or validated general LLM self-repair. Automated review remains separate from human semantic approval.
