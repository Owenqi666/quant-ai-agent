# v0.16 frozen interface: author eligibility study v1

This is a source-specific, label-free eligibility screen, not a return experiment. Existing v0.13–v0.15 contracts remain unchanged. All JSON models are closed/strict, reject nonfinite numbers and bool-as-integer aliases. Canonical digests use storage.digest/json_text.

## Core (root-owned)

`paper_alpha.eligibility_schema` exports `EligibilityPlan`, `EligibilityMonth`, `EligibilityScan`, `EligibilityResult`, `StudyEvidence`.

Plan exact fields:

```json
{"schema_version":1,"source_file":"IntnlData.mat","development_start":"1993-03","development_end":"2006-12","reserved_from":"2007-01","task":"momentum","requires_market_cap":false,"minimum_assets":30,"threshold_origin":"project_screen","rationale":"An explicit project screening threshold, not proof of portfolio feasibility."}
```

source_file = IntnlData.mat|USData.mat; task = momentum|momentum_dgw. minimum_assets integer1..50000. threshold_origin = project_screen|table8_initial_upper_bound; latter requires momentum_dgw, requires_market_cap=true, minimum_assets=450. rationale nonblank1..2000. Development1..180 continuous months with12 prior source months; development_end < reserved_from <= source.last_month; no requirement that end+1 equals reserved_from. Reserved boundary is a declaration, not proof of unseen data.

`EligibilityMonth`: exact `{month, patterns, momentum_missing, momentum_invalid, momentum_unrepresentable}`. patterns has8 nonnegative integer counts. Index bit0=MOM usable, bit1=DGW usable, bit2=MV usable. Sum counts=all original source assets for every month. The three reason counts partition all rows without bit0, precedence: any nonfinite history→missing; otherwise any return<-1→invalid; otherwise finite compounded return cannot be represented→unrepresentable. MOM11 months H−12..H−2; zero retained, −1 valid; DGW/MV at H−1; DGW finite[-1,1], MV finite>0. No H label values/counts in the contract.

`EligibilityScan`: exact `{schema_version:1,kind:"author_eligibility_scan",semantics_version:"gjs-author-eligibility-v1",source:AuthorSource,plan:EligibilityPlan,plan_digest:string,months:EligibilityMonth[]}`. Source exact pinned V2 identity. Max256KiB. Months exactly cover plan. This is a claim derived from a source scan; HTTP cannot authenticate counts from aggregate JSON.

`eligibility.py` public pure functions:
- `validate_plan(dict)->dict`, `validate_scan(dict)->dict` (canonical strict normalized copy)
- `evidence()->dict` (fixed DOI/PDF digest and source/project rule citations)
- `evaluate(scan)->dict` returns `EligibilityResult`
- `render_report(result)->str`
- `demo_scan()->dict` synthetic aggregate test input, **not** raw-file verified

Result exact fields: `{schema_version:1,semantics_version,input_digest,plan_digest,source,plan,evidence,months,summary,limitations,execution_ready:false,verification_scope:"aggregate_consistency_only"}`.
Result month: `{month,assets,momentum_ready,momentum_dgw_ready,momentum_mv_ready,momentum_dgw_mv_ready,selected_ready,threshold_met,momentum_missing,momentum_invalid,momentum_unrepresentable}`.
Summary: `{months,months_meeting_threshold,min_selected,max_selected,status:"screen_passed"|"screen_blocked"}`. screen_passed iff every month meets the predeclared threshold. No month dropping. Evidence exact `{paper_id,doi,pdf_sha256,version,citations:[{id,origin:"paper"|"author_code"|"project",locator,claim}]}`. Limitations are fixed in core.

## Scanner and frozen workflow (data Agent)

`eligibility_archive.scan(path, plan)->EligibilityScan dict` reuses author_archive._verified_archive and scans all original rows once in bounded blocks. Read Return only through last target H−2, DGW/MV only H−1; earlier H values may legitimately enter later history. No repeated per-month file verification. Independent scalar/Fraction test oracle must not call production eligibility math.

`eligibility_workflow.run(plan, output_dir, *, source_path)->dict`, `verify(output_dir, *, source_path=None)->dict`.
CLI: `python -m paper_alpha.eligibility_workflow run --plan FILE --source MAT --out NEW_DIR`; `verify DIR [--source MAT]`.
Freeze plan.json **before scanning** in a new directory, input.json(scan), result.json, source_check.json, environment.json, source snapshot, report.md, manifest.json. Failed attempts leave plan and diagnostic error, never overwrite. Verify re-derives decision/report; with source rescans exactplan/allrows and compares canonical input. Without source only checks artifact/aggregate integrity, never raw recomputation. Receipt must clearly distinguish claimed creation-time raw verification from current re-verification. Reuse existing code_files/environment and file safety patterns. Do not upload rawMAT or change historical artifacts.

## Persistence + HTTP (service Agent)

`AuthorStudies(store)` with create/get/list/markdown/review/reviews. New additive schema12 tables, append-only content addressed study/review records and integrity-bound idempotency receipts. Study IDs `author_study_<64hex>`, review IDs `author_review_<64hex>`.

- POST `/api/author-studies` body `{title,note,scan,parent_review_id:null|string,author_panel_ids:string[],idempotency_key}` →201 `StudyDetail`. title1..200/nonblank, note<=4000, panel IDs<=8 unique, parent ancestry<=32.
- GET `/api/author-studies?limit=20&offset=0` → `{items:StudySummary[],total,limit,offset}`.
- GET `/api/author-studies/{id}` → detail.
- GET `/api/author-studies/{id}/markdown` → computed report; normal report string exactly core.render_report(detail.result); linkage and review remain in detail endpoints.
- POST `/api/author-studies/{id}/reviews` body `{study_digest,decision,note,actor,idempotency_key}` →201 `StudyReview`.
- GET `/api/author-studies/{id}/reviews` → `{items:StudyReview[]}` (bounded<=1000; fail closed above bound).

`StudyDetail` exact fields `{id,digest,created_at,title,note,scan,result,parent_review_id,parent_study_id,author_panel_ids,changes,verification_scope:"aggregate_consistency_only",raw_source_reverified:false}`. `changes` list `{field,before:string,after:string}` where strings are canonical JSON; compare parent/new plan fields, source, and input digest. A revision must change scan; verify parent review and its result binding. New study has no reviews initially; do not copy approval.
`StudySummary` fields `{id,digest,created_at,title,note,plan,summary,parent_review_id,parent_study_id,verification_scope,raw_source_reverified}`.
`StudyReview` exact `{id,digest,created_at,study_id,study_digest,decision,note,actor}`. decision=data_insufficient|rules_unresolved|implementation_error|accepted_with_limits; actor=human|automation. note nonblank1..4000; stale digest409. actor is a local-user declaration, not authenticated identity. Scripts use automation. Link each requested author_panel through verified existing service, matching pinned source and in-plan month. Stored relationships and receipt tampering fail closed. Raw filenames in JSON never cause HTTP filesystem reads/network calls.

API schema Python names: StudyCreate, StudyDetail, StudySummary, StudyPage, StudyReviewCreate, StudyReview, StudyReviews. Generated client updated by coordinator after routes land.

## Basic UI (frontend Agent)

New existing-style section “研究准入”. Import scan JSON (256KiB bound), title/note; optional linked author-panel IDs; optional parent review to register a revised CLI scan. Reuse durable pending submission pattern and single busy guard. Show fixed evidence, task requirements, continuous development/reserved interval, input/plan/source identities, four eligibility columns, threshold result and next unmet method steps. Avoid wording that server independently recomputed raw counts. Review only after visible output; human button action, script test explicitlyautomation. Show exact result binding, existing reviews, revision parent and changes; new study never inherits old approval. Paginate long monthly list locally. No chart needed: no portfolio returns exist.

Root owns generated OpenAPI/TS and release integration. Agents own only files listed in the task plan. Contract changes must be coordinated before implementation.
