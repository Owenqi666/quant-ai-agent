# v0.18 integration contract

## Ownership and migration

Root owns db.py, schema14 wiring, API, frontend and release. Agent A owns service.py mutation guards; B owns research_jobs modules; C owns research_cases projection and research_claims modules. Every new persistence module exports SCHEMA; root installs all statements in _migrate_v13 inside the existing migration transaction. Historic business rows/files remain byte-for-byte unchanged; new guard/index rows are additive. No provider SDK/network dependencies.

## A: dataset temporal guards

Provide a small ResearchGuard(store) service plus transaction-level helpers that Store.create_research/create_revision/submit_run invoke. Guards are keyed by immutable data content, not paper title, research UUID or session. Same data re-registration cannot gain new development access. Freeze reserved test intervals and record observed/declaration ranges conservatively across existing revisions. Protect all data used by computation, including training/trailing history. Do not make a final-test execution endpoint.

Root will call A migration helper after SCHEMA installation to seed historical boundaries without changing original rows or files. A publishes exact helper name before root wiring. Creation/revision/queue acceptance must be atomic with guard enforcement; identical idempotent replay returns original acknowledgment, changed payload conflicts. Legacy unsafe revisions remain preserved but cannot be newly executed. A provides get/list status for UI/API (strict Pydantic schema in research_guard_schema.py).

## B: provider-free research execution jobs

User/application creates a bounded job for an existing research/base revision. Fixed paper/dataset and original temporal guard remain authoritative. Model/script output is untrusted path-free task/candidate data. Reuse Store._task, evidence matching, AST validation, original version creation, run queue, worker and exact get_run verification. Do not implement a second calculator.

ResearchJobs(store) should provide create/get/list, advance with an idempotency key, cancel/stop and an immutable export. Publish strict request/response shapes and SCHEMA early. Define durable job states, original budget, charged transitions and effect keys before any Store mutation. Separate rejection (scientific blocked) from retryable infrastructure failure. Persist original revision/run mutation keys before sending; uncertain completion replays the same keys. The caller cannot replenish job budget. An advance either consumes one bounded transition or safely returns the recorded transition; no indefinite polling or sleep in HTTP.

Minimum transitions: validate structured draft, commit immutable revision, submit existing run, observe queued/running/completed attempt, finish with exact verified result or block/fail. Only allow explicit semantic changes carried by attributed task data; aliases can be normalized by the existing operator registry. Unsupported arbitrary Python/network/tool names are denied. If preflight is blocked or no legal candidate remains, retain reasons and stop; don't silently substitute fields. Real model calls are absent; scripts drive these transitions.

Context projection should expose bounded evidence, fields/operator semantics, base candidate definitions, precise source identity and assessment; omitted material is explicit. Never expose complete final-test data or arbitrary files. Freeze provider-independent JSON actions and responses. Job max steps/failures/elapsed time includes orchestrator work; repeated status reads are not automatic infinite work. Root uses CLI demo and HTTP tests to show the job and the resulting run/ResearchCase. C claims may be linked after the result is complete; B must not depend on claims schema.

## C: assessment and structured claims

Extend newly created case review snapshots with a bounded result-bound ResearchAssessment projection; historical v17 cases and tool receipts must remain readable without changing their frozen bytes/digests. Missing legacy assessment stays unknown. Exact quote matching, source registry reference and semantic human approval remain distinct.

ResearchClaims(store) validates a structured claim set against a case ID/digest. Proposed method shape: preview(case_id, case_digest, claims); create(case_id, case_digest, claims, idempotency_key); get/list/markdown. Publish strict models and SCHEMA early. Each claim has declared kind/attribution, bounded text, evidence IDs, and optional metric references (result_id/digest + bounded JSON pointer). Reference values are filled only by server from verified frozen results; never accept a caller numeric metric value. Free prose and deductions retain semantic_fidelity=unverified unless a separately bound human assessment exists. Do not try to prove arbitrary prose with a regex, and do not count citation membership as truth.

For metric claims generate authoritative numeric display text from references, not caller text; narrative text remains untrusted automation draft. Reject unresolved/foreign IDs, stale result digests, unsupported pointer types and unsafe pointer traversal. Preserve model conjecture vs paper fact vs project convention. A valid claim record is not human approval. Existing four diagnostic tools retain their old contract; root may add separate claims endpoint/UI without changing historic ledger response shapes.

Create v018 semantic cases/reference drafts with explicit pending human confirmation, independent from engineering manifest. Use existing five assessment dimensions. No fabricated reviewer/date/score. Engineering runner must separately state its scope, test bad references/false attribution/untrusted text, and exercise actual ResearchJobs after B publishes API. scripts/evaluate_v018.py can be completed with B once its shapes are stable; keep inputs fixed before results.

## Root integration and acceptance

Use existing FastAPI response schemas/OpenAPI generator and basic UI components. New routes belong to application control plane, not v17 model capabilities. A user-created research job is explicit authorization only within frozen data/paper/budget; review/approval/final test controls remain human/application only.

Add demo_v018 and evaluate_v018 to release acceptance and portable package checks. Verify fresh schema14, schema13 migration, historic case/tool records, guard persistence after backup to a new directory, deterministic baseline equality and failed/cancelled/interrupted attempts. No success is claimed until the actual artifacts and full required checks pass.
