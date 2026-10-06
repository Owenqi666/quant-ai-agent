# v0.6 Alpha101 temporal engineering suite

The user confirmed using *101 Formulaic Alphas* and selected it as project material. The quoted Alpha#101 formula is on page 15 of the bundled PDF; the PDF and extraction hashes are frozen in `manifest.json`. This suite does not add independent papers or human semantic labels.

The three transformations were constructed with AI assistance under the requested project optimization scope. `user_modification` identifies an explicit project change from the cited formula; it does not assert prior personal authorship or human approval. The original formula remains the evidence, while every hypothesis and candidate records the transformation separately.

| Case | Expected behavior | Independent numerical scope |
|---|---|---|
| `temporal_transforms` | Compute a five-session mean, five-session sample standard deviation, and one-session delay of Alpha#101 | Three exact registered formulas; all factor history through validation end plus daily and aggregate evaluation |
| `unsupported_window` | Compute a legal six-session mean | Explicitly unsupported; `not_comparable`, never a numerical pass |
| `missing_field` | Block a declared close-to-vwap substitution because vwap is unavailable | No numerical result expected; no automatic substitute |
| `budget_stop` | Stop the mean candidate after the single preflight tool allowance | `budget_exhausted` with `budget_stopped` candidate |

All four cases are development cases in the same `alpha101` lineage. All three policies receive identical frozen inputs and budgets, and run twice (24 executions total). `agent` is the existing bounded deterministic repair policy with zero LLM calls. None of these expressions require an alias repair, so this suite does not establish an agent advantage.

For each policy, the declared denominators are:

- Task handling: 8 task executions (4 tasks × 2 repeats).
- Positive execution: 8 candidates (3 registered transforms + 1 unsupported-window control, each twice).
- Numerical coverage: those same 8 candidates. A correct run has 6 covered instances; the unsupported control stays in the denominator.
- Numerical agreement: 6 covered instances when all registered cases execute; this conditional rate must be read with coverage.
- Fully verified yield: 6 / 8 on a correct run, reflecting the intentional coverage gap.
- Negative handling: 4 candidates (missing-field and budget-stop cases, each twice).
- Formula lineages: 1, not 4 independent research discoveries.
- Human semantic fidelity and manual time saved: unmeasured.

The split configuration and synthetic dataset are inherited unchanged. The final market test interval remains locked. Standard deviation changes the meaning from directional signal to temporal dispersion; neither a profitable direction nor an economic mechanism is inferred from the original quote.

Run and independently recheck in a new output directory:

```sh
.venv/bin/python -m paper_alpha suite --manifest evaluation_suites/v06/manifest.json --out artifacts/v06-suite-example
.venv/bin/python -m paper_alpha verify-suite artifacts/v06-suite-example
```

Use the release-specific output for acceptance results. Intermediate development output is not release acceptance. Existing v04/v05 manifests are unchanged.
