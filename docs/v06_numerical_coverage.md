# v0.6 independent numerical coverage

## Scope

`stdlib-seven-formulas-v3` extends the four previously registered formulas with exactly three Alpha#101 transformations. The old four calculation branches are unchanged. No arbitrary expression evaluator is added to the reference implementation.

Let `a = ((close - open) / ((high - low) + .001))`, the source formula on page 15 of the bundled *101 Formulaic Alphas* PDF.

| Registered local expression | Definition and first available row |
|---|---|
| `ts_mean(a, 5)` | Complete trailing five observations, including the signal session; arithmetic mean; row 5 |
| `ts_std(a, 5)` | Same complete window, sample standard deviation with `ddof=1`; row 5 |
| `delay(a, 1)` | Previous recorded session's signal; row 2 |

These are project modifications constructed with AI assistance, not three additional original paper formulas. The source citation supports `a` only. The sample-standard-deviation convention comes from the project's frozen operator semantics, not an asserted equivalence to BRAIN. The standard deviation transformation changes directional meaning; usefulness requires human review.

`paper_alpha/server/oracle_temporal.py` uses only standard-library arithmetic and statistics. The production expression interpreter, pandas rolling implementations and production evaluator are not used to construct expected values. `regression.py` continues to independently construct next-open labels, cross-sectional average-tie ranks, gross-normalized weights, daily contributions and aggregate metrics.

Only exact registered ASTs (plus already documented operator aliases and formatting) are covered. A six-session window, altered field, different arithmetic or another combination remains `not_comparable`. The existing maximum of 100,000 market cells and 64 MiB artifact inputs still bounds numerical verification; the new fixed windows inspect at most five signal values per output cell.

## Correctness evidence

`tests/test_oracle_v06.py` checks:

1. Hand-computable mean, sample standard deviation, delay and warm-up; constant-signal standard deviation is zero.
2. A missing input invalidates exactly its complete trailing windows or delayed output and recovers at the correct session.
3. Prefix computation and changes confined to future rows preserve past reference and production outputs.
4. Actual worker artifacts for all three formulas agree on factors, daily labels/ranks/weights/contributions, purged dates and aggregate metrics.
5. Coherent corruption of both state and result files still fails independent checking: incorrect forward return, weight, aggregate metric or same-session entry.
6. Moving tomorrow's factor values into today's rows fails independently for every new formula, even with the date grid intact.
7. A legal changed-window candidate executes but remains unsupported by the oracle.
8. Suite denominators retain uncovered executable candidates and keep all variants in one lineage.

These extend, rather than replace, the existing four-formula tests in `tests/test_regression_contract.py`. The hand-calculation panel tests exercise operator arithmetic directly; market admission still requires the existing strict synthetic OHLCV dataset contract. Missing-input tests do not imply missing real-market rows are now accepted for end-to-end runs.

```sh
.venv/bin/python -m unittest tests.test_regression_contract tests.test_oracle_v06 -v
```

## Evaluation and remaining limits

The v06 manifest has four tasks, three deterministic policies and two repeats: 24 executions, all from the same paper and formula lineage. The explicit unsupported six-session case intentionally limits numerical coverage to 6 / 8 positive instances per policy when execution succeeds. See `evaluation_suites/v06/README.md` for every denominator.

Historical v04/v05 inputs and manifests remain frozen. The oracle version changes because coverage changes; old acceptance records keep their original oracle version. To assess the current verifier, create a new run and verification output rather than rewriting historical reports.

No real-market data, human semantic approval, active human research time, model advantage, investment performance, transaction-cost realism or BRAIN equivalence is established by these checks. Expanding the formula registry further should follow a concrete research need and an independently reviewed bounded reference implementation.
