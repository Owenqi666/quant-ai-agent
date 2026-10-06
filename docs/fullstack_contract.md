# Full-stack delivery contract

This increment is a local, single-user research workbench. No model calls, keys,
RAG, real-market data, or BRAIN connection. Existing deterministic engine remains
the numerical authority. SQLite WAL is the durable single-host store; a separate
worker executes jobs. This is not a public multi-user service.

v0.5 adds explicit response models and generated OpenAPI/TypeScript contracts;
see [response contract and generation checks](v05_api_contract.md). They validate
the HTTP projection without rewriting saved scientific results. The human workflow
now uses basic forms, explicitly restored local drafts and readable record selectors.

## HTTP contract

All routes below are under `/api`. JSON errors use `{detail: string}`. Objects use
UUID strings and UTC ISO timestamps. Legacy lists return arrays; v0.4 pages use explicit envelopes. API never accepts a
filesystem path. Research edits create immutable revisions with optimistic
concurrency. Server fills the engine's paper/data paths.

- `GET /health`: `{status, version, database_schema, workspace_id, ai_enabled:false, worker: {online,last_seen}}`.
- `GET /capabilities`: explains AI disabled, synthetic-only, local single user.
- `GET /papers`; `POST /papers` multipart `file`, `title`; `GET /papers/{id}`
  includes extracted pages; `GET /papers/{id}/pdf` streams stored PDF.
- `GET /datasets`: registered immutable synthetic versions, fields and metadata.
- `POST /examples/alpha101`: idempotently imports the bundled paper and creates a
  research/revision. Returns `{research_id, revision_id}`.
- `GET /researches`; `GET /researches/{id}` returns research with `revisions`.
- `POST /researches`: `{title,paper_id,dataset_id,task}`. Task has `evidence`,
  `hypotheses`, `candidates`, `evaluation`, `budget`; excludes filesystem paths.
- `POST /researches/{id}/revisions`: `{base_revision_id,task,note}`; 409 on stale
  base, returns revision. Previous revisions and outputs are unchanged.
- `POST /runs`: `{revision_id,mode,idempotency_key}`. Modes `agent`, `fixed`,
  `normalized_fixed` (UI calls agent "bounded repair", no AI). Reusing a key for
  another request is 409. Run includes id, revision_id, research_id, status,
  mode, created_at, started_at, finished_at, error, attempt_count.
- `GET /runs`; `GET /runs/{id}` includes `state`, `verification`, `reviews`, and
  `attempts`. Exposes only JSON artifacts, never absolute internal paths.
- `POST /runs/{id}/cancel`; `POST /runs/{id}/retry`: only failed/interrupted runs
  may retry, capped; creates a fresh attempt directory, preserves earlier output.
- `GET /runs/{id}/events?after=0`: ordered persisted event array; polling is the
  initial progress transport (no WebSocket dependency).
- `GET /runs/{id}/report`: Markdown download; `GET /runs/{id}/artifacts` returns
  `{id,name,size,sha256}` array; `GET /runs/{id}/artifacts/{id}` downloads only
  registered verified artifacts (no user-supplied file paths).
- `POST /runs/{id}/reviews`: `{candidate_id,verdict,category,note,source}`; append-only,
  terminal verified results only. Verdict accepted/needs_changes/rejected.
  Category evidence/hypothesis/implementation/data/evaluation/other. Review binds
  exact revision, attempt and result digest. No automatic review approval.
- `GET /regression-cases`; `POST /regression-cases`:
  `{review_id,expected_status,note}` explicitly approves a versioned development
  regression case. Expected candidate status is human entered, not inferred.
  Version 2 adds `contract` and `contract_digest`: frozen evidence/hypothesis,
  alias-normalized formula, data identity/semantics, evaluation configuration,
  and operator/execution semantics read from the reviewed source snapshot.
  Legacy cases keep null contracts; approving the original review creates a
  new record rather than rewriting the historical approval.
- `POST /regression-checks`: `{run_id,case_ids}` checks research/paper/dataset/
  candidate identity and frozen scientific contracts. `outcome` is `passed`,
  `failed` or `not_comparable`; legacy `passed` remains true only for full pass.
  Results include `compatible`, `differences`, and per-check `name`, `outcome`,
  `reason`, with optional expected/actual or numerical mismatches/tolerances.
  Evaluated outputs require literal evidence and a supported independent
  numerical reference; unsupported formulas are not comparable. Non-evaluated
  negative cases check expected status only. Checks preserve target revision,
  attempt and state digest; no automatic economic-fidelity judgment is made.
- `GET /regression-checks` returns stored checks.

## v0.4 additive contracts

Database schema 3 migrates supported schema 1/2 in order, preserving original
records. Review `source` is human/automation/imported/legacy_unknown (legacy
client default). It declares provenance; it does not authenticate a person.

- `GET /runs/{id}/events/page?after=0&limit=200&through=N` returns
  `{run_id,items,next_cursor,has_more,high_watermark,total_records}`. Snapshot and
  rows share a read transaction. IDs can have gaps; never use engine_sequence.
  Limit 1..1000. Out-of-range cursor/watermark is 409; malformed bounds 422.
  Omit through for a new catch-up snapshot. Old event array remains unchanged.
- `POST /dataset-imports`: multipart `file` (CSV), `metadata` (JSON file), `title`,
  `idempotency_key`; saves original bytes before parsing. Per-file limits are
  16/2 MiB, total plus 64 KiB overhead, including chunked bodies.
- `GET /dataset-imports`, `GET /dataset-imports/{id}` return receipts with
  id/title/status/created_at/updated_at/csv_sha256/metadata_sha256/input_digest,
  latest_validation_attempt_id/registered_dataset_id/error/latest_validation,
  validation_attempts/registration_attempts/events. Paths are derived from IDs.
- `POST /dataset-imports/{id}/validate`: `{idempotency_key}`. Synchronous bounded
  subprocess; disconnect does not cancel it. Query receipt after uncertainty.
  Report includes schema/validator digest/input digest/status/checks/summary,
  duration/limitations and fail-fast diagnostics; old attempts remain.
- `POST /dataset-imports/{id}/register`: `{idempotency_key,input_digest,
  validation_attempt_id,report_digest}`. Revalidates bytes and current validator
  identity, publishes complete files before short registration transaction.
  uploaded → validating → valid/invalid/interrupted → registering → registered.
  Same key/different request conflicts. Recover only after proving no lock owner.
- `GET /datasets/{id}` adds registration kind validated_import or
  legacy_registration, with originating receipt IDs. A research's dataset_id is
  immutable; select another data version by creating another research.
- Research detail adds `preflight` for the latest revision: candidate-specific
  static field/expression/warm-up results. It never alters the submitted task.
- `POST /issues`: `{review_id,note,source,disposition,idempotency_key}` opens a
  problem. `GET /issues/{id}` returns append-only history and latest_event_id.
- `POST /issues/{id}/events`: `{base_event_id,state,disposition,note,source,
  idempotency_key,revision_id?,target_run_id?,check_id?}`. States open/proposed/
  awaiting_review/resolved/deferred. Dispositions implementation_fix,
  hypothesis_change,data_change,accept_limitation. Server freezes
  target_attempt_id/target_result_digest. Resolving needs linked verified target;
  implementation fixes additionally require compatible passed cases approved
  from the original review and a check for the exact result. Unrelated failing
  cases do not grant or revoke this issue's proof. No automatic resolution.
- `GET /catalog/{resource}` pages runs/researches/reviews/regression-cases/
  regression-checks/issues. after/through as above, limit 1..200 default 50.
  Supports research_id/dataset_id/candidate_id. status applies to runs/issues/
  checks; category/source to reviews/issues. Invalid filter dimensions are 422.
  Append boundary is fixed; refreshing is needed for mutable status changes.
- `GET /feedback-summary?research_id=...` returns metrics, exact inputs,
  input_digest, filters, skipped_attempts and timing observations. Bounded to
  500 cohort runs. Related cross-research proof is verified separately and
  excluded from coverage denominators. Historical verified attempts remain
  available even when the latest attempt is queued. `GET /feedback-summary/export`
  accepts the same filter plus `format=json|csv`; JSON preserves full inputs.

Regression PDF/reference work uses a WAL read snapshot. Publication rechecks
the exact attempt/result/case rows and artifact hashes under a short writer
transaction; a concurrent change fails with 409. Stored timings separate this
work from publication revalidation. Old checks are not silently recomputed.

## Backend collaboration interface

Package `paper_alpha/server/` owns `db.py`, `service.py`, `api.py`, `schemas.py`.
Root owns `runner.py` and top-level delivery/integration files. Store's public
worker methods: `claim(worker_id) -> run dict | None`, `heartbeat(worker_id,
run_id=None)`, `finish(run_id,worker_id,attempt_id,status,error=None,
verification=None,timings=None)`, `cancel_requested(run_id) -> bool`, `recover_stale()`.

Claim atomically transitions queued to running, increments attempt_count, creates
attempt UUID, and returns `attempt_id`, `task_path`, `output_dir`, `mode` in addition
to run metadata. Paths are server-owned. At most one live worker may execute a
run. A global worker file lock fences process execution on this local host;
expired heartbeat marks runs interrupted, not silently successful. `finish`
checks owner, attempt and running/cancelling state, and may not overwrite a newer
attempt. Terminal cancellation wins over a late success. Worker creates separate
OS process group, enforces engine budget plus bounded supervisor grace, kills
group on cancellation/shutdown, verifies results before publishing completed.

Store(root: Path), root is application state directory. `store.db_path` is
SQLite file. API factory `create_app(root: Path | None = None)`; default root
`PAPER_ALPHA_HOME` or repo `var/workbench`. Worker CLI:
`python -m paper_alpha.server.runner --home PATH [--once]`.
State transitions and recovery are covered by backend tests in `tests/`.

## First acceptance workflow

Load demo -> inspect paper/candidates -> queue experiment -> inspect metrics and
failed candidates -> annotate a result -> approve a development regression case
-> create a revision -> run again -> execute and inspect saved regression checks.
Also test idempotent submit, stale revision conflict, invalid evidence, worker
interruption/retry, cancellation, artifact tampering and AI-disabled behavior.


## v0.6 兼容扩展

ReviewCreate 可选 assessment；结构化审核强制带 expected_attempt_id 和 expected_result_digest，事务内绑定冻结候选。RunDetail 的 review_targets 提供完整目标摘要，不能从裁剪后的展示状态自行计算。ReviewResponse 的 assessment 可为 null，assessment_summary 分别表示声明的语义状态和是否记录主动时间。旧通用审核不自动变成分项通过。

数据验证报告支持 v1 历史结构和 v2 结构化定位；行号、字段、日期/资产、样本和 related_rows 有界，元数据或缺网格不伪造实际行。完整语义见 v06 专项协议。所有新增响应继续纳入 OpenAPI 生成与运行时校验。

## v0.7 提交恢复扩展

ReviewCreate / CaseCreate 可选 `idempotency_key` 保持旧调用兼容；新前端总是提供。同操作同键同内容回放原成功响应，异内容 409。业务记录和收据在同一事务提交。

ReviewCreate 还允许成对提供 `expected_attempt_id` / `expected_result_digest`。新前端所有审核都提供，首次提交在事务中验证；已过期的新前置条件返回 412，未写入收据。仅使用历史 assessment 内目标的旧调用仍保留 409。顶层与 assessment 目标不一致或字段不成对为 422。成功回放先于当前产物校验，确认的是原操作而非当前完整性。成功业务响应结构和 201 均不变。

草稿和待确认请求是浏览器本地恢复状态，按工作区及操作/冻结结果隔离，不当作服务器真相。来源和时间口径沿用 v0.6。具体协议见 [v07_idempotency.md](v07_idempotency.md) 与 [v07_review_recovery.md](v07_review_recovery.md)。

## v0.8 研究提交、对比与快照

研究创建增加可选幂等键；运行提交保留原来的同键确认语义。前端先持久保存固定请求，再发 HTTP，确认后按原操作范围清理。旧组件迟到响应不能覆盖或清理新的请求与草稿。详细重放、冲突和迁移边界见 [创建契约](v08_submission.md)。

- `GET /api/run-comparison?baseline_run_id=...&candidate_run_id=...` 返回保存的条件、候选覆盖、来源和有条件的差值。
- `POST /api/researches/{id}/reports` 接受明确的 `run_ids` 和 `idempotency_key`，创建或确认同一冻结报告。
- `GET /api/researches/{id}/reports?limit=20&offset=0` 分页读取报告目录；`GET /api/research-reports/{id}` 读取完整已验证快照。
- `GET /api/research-reports/{id}/export?format=json|markdown` 导出同一持久快照，摘要异常拒绝输出。

条件未知或不兼容不生成差值；报告源核验与报告自身摘要分开。请求、响应模型由生成契约校验。资源上限与快照范围见 [研究分析契约](v08_research_insights.md)。
