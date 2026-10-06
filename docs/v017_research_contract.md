# v0.17 research coordination contract

## Scope

Provider-free, local single-user research context and diagnostic/proposal tools. Existing domain services remain the authorities for their own results. No tool may import model-supplied counts, calculate portfolios from an eligibility result, write human approval, lower a threshold or access a final test.

## Research cases (Agent A)

`ResearchCases(store)` exposes:

- `preview(source_kind, source_id)` resolves and verifies an existing domain result and returns its frozen context, exact source digest and current decision.
- `create(title, note, source_kind, source_id, source_digest, idempotency_key)` freezes the preview, refusing changed results.
- `get(identity)`, `list(limit=20, offset=0)`, `markdown(identity)`.

Source kinds: `daily_run`, `monthly_experiment`, `author_study`. Result-bearing completed daily/monthly attempts only; never silently bind a future/latest retry. Author screen outcomes are both completed calculations, including `screen_blocked`. Case identity/digest bind immutable context, distinct source identity, method/data scope, evidence, project conventions and result references. Source metadata that cannot be established remains explicitly unknown. Do not infer independent semantic fidelity or human review from literal quote checks. Existing review snapshots retain exact scope and actor; no approval transfer.

Case detail has `id`, `digest`, `created_at`, `title`, `note`, `source_kind`, `source_id`, `source_digest`, and `context`. Context must provide `state`, `stop_reason`, `allowed_actions`, and bounded evidence/result/provenance for the tool adapter. Agent A will publish final strict shapes before frontend integration. Suggested actions are a fixed enum: `request_human_review`, `revise_plan`, `resolve_method`, `stop_data_insufficient`; no `execute_author_portfolio`.

HTTP (root wiring): GET `/api/research-cases/preview?source_kind=...&source_id=...`; POST/GET `/api/research-cases`; GET `/api/research-cases/{id}` and `/markdown`. POST has the create arguments above. Models live in `research_cases_schema.py`.

## Restricted tools and sessions (Agent B)

`ResearchTools(store)` owns its strict HTTP schema and a `SCHEMA` constant (included in A's schema13 migration). One session binds an existing immutable case ID/digest and a fixed budget; session restore never increases its budget.

Public tools: `read_case`, `read_evidence`, `read_result`, `propose_next_action`. Reads re-verify the case and return server-produced context/results. Proposal arguments select an action from the case's allowed actions, bounded rationale and evidence IDs; rationale is unverified automation text. A proposal never updates a source result, human review, threshold or run status. All calls use explicit idempotency keys; same-key requests with changed payloads conflict. Unknown/disallowed tools are denied and recorded without executing a mutation. Tool response distinguishes technical errors (stable code, retryable) from successful scientific blocking. Budget and interrupted calls remain charged across restart. Sequential operation ownership must prevent concurrent double spending.

Methods/routes to publish by Agent B: capabilities, create/list/get session and call tool. Schemas must work with existing OpenAPI generator (closed Pydantic models, no new JSON Schema vocabulary without coordination). No arbitrary files, network URLs, Python or SQL input. No provider SDKs or new dependencies.

## Evaluation (Agent C)

Freeze policy cases separately from implementation: expected action/state, allowed evidence, disallowed capability, budget behavior and scope. Label these as engineering/policy expectations, not human semantic ground truth. Include human-confirmation fields left pending where needed. Positive path uses actual existing controlled calculation; negative path uses explicit synthetic scan fixture or optionally a saved actual-source scan. Reuse existing daily/monthly execution and author studies rather than invent results inside the restricted tools.

`scripts/demo_v017.py --out NEW_DIR` provides isolated end-to-end proof; `scripts/evaluate_v017.py --out NEW_DIR` runs fixed declared expectations versus bounded scripted tool use, saving per-case input, outputs, call records and aggregate report. Do not overwrite outputs or operate live home. All decisions/numbers in reports must correspond to saved tool or evaluation outputs. No human-time/model-cost claims without measurements.

## Migration and ownership

Schema13 adds case records/receipts plus the session/tool ledger. Agent A owns `db.py`, importing B's SCHEMA inside `_migrate_v12`. Root wires API routes, generated client, minimal UI and version/release changes. Agents coordinate schema changes before use. New records use existing canonical digest conventions and fail closed on tampering. Previous business records and immutable files must remain unchanged.

## Frozen case projection (Agent A)

Implemented in `server/research_cases_schema.py`. `CasePreview` has source kind/id/digest and context; `CaseDetail` additionally has id/digest/created_at/title/note. `source_digest` is the canonical digest of `{source_kind, source_id, context}`, so preview-to-create rejects changed results or reviews. Case reads verify the frozen review subset; later reviews do not change an old case. A different attempt/result invalidates the binding, never silently redirects it.

Context fields: `state`, nullable `stop_reason`, `allowed_actions`, `evidence`, `definitions`, `data_scope`, `method_scope`, `provenance`, `results`, `reviews`, `limitations`. Evidence contains `{id, origin, locator, text, verification}`; registry paraphrases are not called quotations. Definitions contain `{id, attribution, text, evidence_ids}`. Provenance contains `{label,value}`. Results contain `{id,kind,digest,summary,payload_json}` with canonical server-derived JSON. Reviews contain `{id,actor,decision,scope,note}` and retain exact original result scope. Detail is bounded to 4 MiB; individual result JSON is at most 2 MiB. `CasePage` is a lightweight summary plus total/limit/offset.

Deterministic next-action mapping:

| Source result | State | Allowed actions |
|---|---|---|
| Daily with at least one evaluated candidate; monthly evaluated/partial | `ready_for_review` | `request_human_review`, `revise_plan` |
| Author screen_blocked; monthly not_evaluable; daily all candidates blocked | `data_insufficient` | `stop_data_insufficient`, `request_human_review`, `revise_plan` |
| Author screen_passed (portfolio method absent); monthly blocked (rules unresolved) | `rules_unresolved` | `resolve_method`, `request_human_review` |
| Completed daily attempt with no evaluated candidate and non-data failures | `implementation_failed` | `request_human_review`, `revise_plan` |

Actions are suggestions for human review, never source mutations or automatic threshold changes. Existing human/automation labels remain unauthenticated local declarations. The projection does not establish independent semantic fidelity or author-source portfolio execution.
