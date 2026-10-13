# Public CI

The public workflow runs one configured combination: Ubuntu 24.04, Python 3.14,
and Node 24. It installs `requirements-web-lock.txt` and `frontend/package-lock.json`,
checks generated API contracts, discovers every `tests/test_*.py` in the curated
public checkout, and checks frontend types, build and tests. The core test runner
records each method's outcome and fails on an empty collection, skips or expected
failures. The fixture Agent demo and its independent artifact checker run afterward.
The workflow records the actual interpreter versions in its uploaded logs.

Core integration tests use original teaching PDFs, bundled synthetic market data
and fresh temporary workspaces. They cover idempotency, concurrent submission,
replay, controlled Worker interruption/retry, backup integrity and actual Worker
execution after restoration. Original inputs, attempts and saved outputs remain
in history. The interruption test is a controlled stop, not an operating-system kill.

The correlation matrix compares the retained reference and production within the
runner's own environment: axes, dtypes, NaN masks and non-NaN bits, including signed
zero, plus chunk seams, layouts and downstream behavior. The separate small
benchmark records warmed alternating timings, fresh-process peak RSS and exact
fallback counts. Numerical differences or execution errors fail CI; noisy elapsed
times have no absolute or speedup threshold. Large cases run locally through the
[benchmark entry point](ts-corr-benchmark.md).

The agent demonstration substitutes fixture model responses. CI requires no model
account, credential, external paper download, private database, or old research
workspace. Successful calculation is software evidence; human research quality,
real model quality, and investment effectiveness remain unassessed.

Local reproduction, from a clean public checkout:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-web-lock.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python scripts/export_api_contract.py --check
PUBLIC_RELIABILITY_ARTIFACT_DIR=.ci/reliability \
CORR_COMPATIBILITY_REPORT=.ci/corr-compatibility.json \
  .venv/bin/python scripts/run_public_tests.py --output .ci/python-tests.json
.venv/bin/python scripts/benchmark_ts_corr.py --suite smoke \
  --warmups 5 --repeats 5 --output-dir .ci/ts-corr-smoke
(cd frontend && npm ci && npm run typecheck && npm run build && npm test)
.venv/bin/python scripts/demo_public_agent.py --output-dir .demo/agent
.venv/bin/python scripts/check_public_agent_demo.py --output-dir .demo/agent
```

Use new output directories for another demo or benchmark run. A local pass and
a remote Actions pass are separate observations; a workflow file alone does not
establish either.
The artifact upload runs even after a failed check and includes only logs, selected
original fixture inputs, proposal snapshots, test outcomes, correlation difference
records, benchmark JSON/reports and demonstration reports. It excludes workspace
databases, grants, account configuration, and credentials.

Actions are pinned to full commit identifiers, with `contents: read` and no
`pull_request_target` event. The pinned versions were checked against the official
[checkout](https://github.com/actions/checkout/releases/tag/v7.0.1),
[setup-python](https://github.com/actions/setup-python/releases/tag/v7.0.0),
[setup-node](https://github.com/actions/setup-node/releases/tag/v7.1.0), and
[upload-artifact](https://github.com/actions/upload-artifact/releases/tag/v7.0.2)
releases. This workflow describes its configured checks; consult each run's status
and uploaded logs for the observed result. [Publication checks](publication-validation.md)
link the accepted stages of the current PR separately from historical results.
