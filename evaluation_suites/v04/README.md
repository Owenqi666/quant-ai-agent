# v0.4 development engineering suite

The versioned manifest declares four development tasks, three deterministic
policies, two repetitions, and one **pending reserved-material placeholder**.
It does not contain a completed independent research holdout or any confirmed
user-study labels. Repeated runs and alias spellings do not add independent
research hypotheses. All market calculations remain restricted to validation;
the market final-test interval stays locked.

- `robustness`: the existing eight positive/negative candidate controls, reused
  from the declared fixed-fixture benchmark.
- `extra_formulas`: Alpha012, an explicitly modified Alpha033, and an executable
  formula deliberately unsupported by the numerical reference (`open`).
- `wrong_quote`: a deliberately absent citation; expected to block computation.
- `budget_stop`: a one-tool budget; expected to preserve budget exhaustion.

The bundled PDF and extracted pages were checked against each other when these
fixtures were prepared. Alpha012 is quoted on PDF page 9. Alpha033 on PDF page 10
contains exponent one; its local expression explicitly removes that exponent and
is attributed as `user_modification`, not as a byte-for-byte original formula.
The example paper's personal study by the user is unconfirmed. Expected handling
labels were constructed with AI assistance for engineering verification; they
are not human judgments of economic fidelity.

The independent reference uses the Python standard library and supports only
its four registered exact formulas under its declared operator semantics. It
recomputes factor history and validation labels, weights and daily/aggregate
results. It is not a second general expression engine. A zero-signal negative
control and the legal `open` expression remain explicitly unsupported reference
cases. Unsupported is retained in coverage denominators and is never a numeric
pass. Float comparisons declare tolerances; discrete counts and identities are
exact.

`paper_alpha.evaluation_suite.load_manifest(path)` validates every frozen task
and input digest, candidate/quote inventory, expected outcomes and aggregate
budget. `run_suite(path, new_output_directory)` executes development tasks only;
reserved declarations cannot be run through this entry point. The same task and
budget are used for fixed, normalized_fixed and bounded repair. Repetitions use
rotated policy order. Existing output directories are refused.

Artifacts retain the original manifest bytes, materialized task/input copies,
original task bytes, per-policy attempts, source/environment hashes, progress,
summary and a result-derived report. `summary.json` carries per-item IDs for
every ratio. An interrupted run retains progress and all remaining planned runs;
no missing run may be dropped to improve a denominator.

`acceptance_passed` means declared engineering handling and repeat consistency
within the declared coverage passed. Read it alongside `fully_covered`, numeric
coverage and full-verification yield; it does not mean all formulas have an
independent oracle. The strongest current baseline is normalized_fixed, which
uses the identical alias transformation upfront. No strategy advantage, human
time saving, LLM benefit or investment performance is presumed.

Before adding a real reserved research task, obtain explicitly sourced labels,
assign its paper/formula families, freeze a new manifest version, and keep it out
of development. This loader conservatively rejects a shared paper or declared
family across development/reserved partitions. Changing a dataset also requires
new explicitly approved workbench cases; this CLI suite does not relax the
workbench's scientific compatibility checks.

`verify_suite(output_directory)` independently reopens the saved run artifacts,
checks task/input/code bindings across policies, recomputes the registered
reference checks and all ratio denominators, and verifies `summary.json` and the
rendered report against them. It does not authenticate label authors or certify
wall-clock logs as human effort. A report produced with a different verifier or
oracle version requires that explicitly recorded version or a new verification.
