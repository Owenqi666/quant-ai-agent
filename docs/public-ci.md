# Public CI

The public workflow runs one configured combination: Ubuntu 24.04, Python 3.14,
and Node 24. It installs `requirements-web-lock.txt` and `frontend/package-lock.json`,
checks the generated API contracts, runs eight public Python test modules,
including the original reliability and demo regressions, and checks frontend types,
build, and tests.
The fixture agent demonstration and its separate artifact checker run afterward.
The workflow records the actual interpreter versions in its uploaded logs.

The reliability tests create their own small PDF and fresh temporary workspace.
They verify request idempotency, concurrent submission, completed-run replay, and
a controlled Worker stop followed by explicit retry. The interrupted attempt and
original inputs remain in history. This does not test an operating-system kill.
The tests use bundled synthetic market data and an empty Provider registry.

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
PUBLIC_RELIABILITY_ARTIFACT_DIR=.ci/reliability .venv/bin/python -m unittest -v \
  tests.test_expressions tests.test_evaluation tests.test_formula_presentation \
  tests.test_paper_segments tests.test_formula_engineering_material \
  tests.test_operator_definition_public tests.test_public_reliability \
  tests.test_public_agent_demo
(cd frontend && npm ci && npm run typecheck && npm run build && npm test)
.venv/bin/python scripts/demo_public_agent.py --output-dir .demo/agent
.venv/bin/python scripts/check_public_agent_demo.py --output-dir .demo/agent
```

Use a new output directory for another demo run. A local pass and a remote Actions
pass are separate observations; a workflow file alone does not establish either.
The artifact upload runs even after a failed check and includes only logs, selected
original fixture inputs, proposal snapshots, and reports. It excludes workspace
databases, grants, account configuration, and credentials.

Actions are pinned to full commit identifiers, with `contents: read` and no
`pull_request_target` event. The pinned versions were checked against the official
[checkout](https://github.com/actions/checkout/releases/tag/v7.0.1),
[setup-python](https://github.com/actions/setup-python/releases/tag/v7.0.0),
[setup-node](https://github.com/actions/setup-node/releases/tag/v7.1.0), and
[upload-artifact](https://github.com/actions/upload-artifact/releases/tag/v7.0.2)
releases. This workflow describes its configured checks; consult each run's status
and uploaded logs for the observed result.
