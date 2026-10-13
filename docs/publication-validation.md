# Publication checks

## Current PR: public tests and correlation update

[PR #3](https://github.com/Owenqi666/quant-ai-agent/pull/3) records the current head,
checks and artifacts. Its test/backup stage passed
[Public CI 38430037990](https://github.com/Owenqi666/quant-ai-agent/actions/runs/38430037990).
The numerical stage passed
[Public CI 38456656929](https://github.com/Owenqi666/quant-ai-agent/actions/runs/38456656929)
for source head
[`3fcaadbd2fc5aa4c6abe13f164e3b8eda2953e73`](https://github.com/Owenqi666/quant-ai-agent/commit/3fcaadbd2fc5aa4c6abe13f164e3b8eda2953e73);
Ubuntu tested PR merge checkout `5fdae00655794915a7668e68618579ce5da66b4e`.

| Environment-local numerical stage | Observed result |
| --- | --- |
| Isolated macOS public checkout, Python 3.14.8 | 165 core Python tests, zero skips; 243 correlation comparisons, zero NaN-mask or non-NaN bit differences |
| Ubuntu 24.04 Public CI, Python 3.14.8 | 165 core Python tests, zero skips; 243 correlation comparisons, zero differences; frontend, API contracts, Worker demo and independent checks passed |
| Apple M5 Pro operator benchmark | Nine cases including the large panel; five warmups and five alternating repeats per implementation, independent process RSS and separate fallback/profile runs; clean recorded source unchanged |

Each platform compares reference and production in its own locked environment;
ordinary floating arrays from macOS are not an Ubuntu oracle. The matrix includes
signed zero and downstream regressions. This is finite compatibility evidence,
not a proof for every legal input. [Benchmark method, results and raw evidence](ts-corr-benchmark.md)
describe measured computation performance rather than investment or research gains.

This page lists stage evidence. For the final cumulative version's status, actual
PR head and tested checkout SHA, consult
[PR #3](https://github.com/Owenqi666/quant-ai-agent/pull/3) and its corresponding CI
runs. Detailed method mappings and stage logs stay in the PR and CI artifacts.

## Historical public patch: 13 October 2026

The results in this section belong to
[commit af07a86efbe6bd5ad56f6bd98ca0feec80679135](https://github.com/Owenqi666/quant-ai-agent/commit/af07a86efbe6bd5ad56f6bd98ca0feec80679135).
Its [successful Public CI run 38325161941](https://github.com/Owenqi666/quant-ai-agent/actions/runs/38325161941)
was triggered by `pull_request` for that exact head SHA. Later release-closeout
commits and merged `main` require their own CI evidence.

A separate public checkout installed the locked dependencies in a new Python
3.14.4 environment and used `npm ci` with Node 24.16.0 on macOS. It passed
103 public Python tests, 505 frontend tests, TypeScript checking, the production
build and the generated API contract comparison. The account-free Agent demo
completed one original Worker run; its independent checker passed all 13 checks.
A malformed fixture reply remained failed with `REQUEST_MISMATCH`. Browser
readback and screenshots recorded for that revision used its generated workspace,
not an old research database. Human scores, confirmations and timing measurements
remained empty.

The [workflow](../.github/workflows/public-ci.yml) passed on a fresh Ubuntu
24.04 / Python 3.14.8 / Node 24.21.0 runner for the linked revision: 103 public
Python tests, 505 frontend tests, API contract comparison, TypeScript checking,
production build, the Worker demo and all 13 independent checks. This remote
result is separate from the local checks above. Selected logs and
reproducible demo artifacts are uploaded even on failure. The source definition
regressions exercise full supported RHS grammar, altered suffixes, dependency
coverage and preserved repair versions; they do not establish general semantic
entailment.

For that patch, the maintained application and public export shared the same
production fix. The older frozen Runtime archives and their signoff records were
preserved; they do not contain that later patch.

## Historical 0.27.2 source publication

Checked on 12 October 2026 in the isolated 0.27.2 public source directory using existing local dependencies: macOS, Python 3.14, Node.js 24.16.0. Dependencies were reused for efficiency; a fresh network installation and another operating system were not re-qualified.

| Check | Result |
| --- | --- |
| Native transport identity, selected PDF spans, span catalog, formula-engineering material, and curated finite definition tests | 46 passed across 5 modules |
| Formula-engineering response, persisted experiment, and historical readback tests | 3 passed after local Alpha101 preparation |
| Material configuration, material adoption, nullable hypotheses, and research creation frontend tests | 18 passed in 4 files |
| Generated OpenAPI / TypeScript contract comparison | Passed without rewriting contracts |
| TypeScript check and Vite production build | Passed; existing large-chunk warning retained |
| Original public CLI demo | Prepared synthetic inputs, computed an evaluated candidate, verified saved outputs |
| New grounded-material and formula-engineering contracts | Frozen resource hashes verified |
| Native profile source closure | All 119 declared source inputs present |
| Launcher smoke check | Temporary empty workspace, separate free loopback port, version 0.27.2/schema 29, Worker online, model dispatch off, built frontend served; stopped after check |
| Public inventory | Explicit source selection, external-paper/full-text and private-record exclusion, sensitive-pattern inspection, and document link inspection |

The first formula-engineering fixture check exposed an old synthetic source that lacked the complete named formula required by the newer source-support gate. Only the public test fixture was completed with an original toy formula and matching claim; the validator and original development tests were unchanged. The selected tests then passed.

These are bounded publication checks, not the full project test suite, remote CI, new real-model calls, real-market research acceptance, or measured human efficiency. Temporary workspaces, locally prepared external documents, environment links, and raw validation outputs are outside the published inventory.

The 0.27.2 local Runtime has separate frozen software, real-model, numerical, and restart evidence. Its recorded live journey covers selected-page extraction and candidate generation for one previously seen Alpha012 case using synthetic data; independent numerical consistency and restart/readback checks passed. The final extraction already had complete definition bindings, so its first automation version records a zero-change check. Actual missing-definition repair is separately evidenced by a preserved historical counterexample replay. These observations do not establish general paper understanding or human research quality. Human approval and efficiency measurements remain pending/unmeasured.

The curated public source changes documentation and test fixtures, omits private/source-restricted material, and retains the original tutorial. It is not a byte-identical redistribution of the accepted Runtime archive.
