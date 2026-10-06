import {test, expect, type Page, type Route} from "@playwright/test";
import type {CaseDetail, ClaimDetail, ClaimReviewTarget, IndustryMomExperimentDetail, IndustryMomSourceDetail, IndustryMomExperimentSummary} from "../src/generated/api-contract";
// Synthetic HTTP/UI contract fixtures only: not downloads, actual strategies, authorship or human labels.
const h = "a".repeat(64), input = "b".repeat(64), panel = "c".repeat(64), sourceId = `industry_mom_source_${h}`;
const eid = "industry-browser-fixture", aid = "industry-attempt-fixture", cid = `research_case_${h}`, claimsId = `research_claims_${h}`;
const time = "2026-10-06T12:00:00+00:00", limitation = "Browser automation fixture; no official download, market evaluation or human approval.";
const labels = ["引用与页码准确", "假设忠于所引原文", "经济解释归因恰当", "字段含义与替代说明", "实现与声明公式一致"];
function result() {
  const metric = {months_total: 1, months_evaluated: 1, mean_gross_return: 0, annualized_sample_volatility: null, annualized_mean_over_volatility_zero_rf: null,
    terminal_gross_return_index: 1, max_gross_drawdown: 0, cumulative_complete: true, excluded_months: []};
  const strategy = {status: "evaluated", weights: [{asset: "Food", weight: 0.5}], gross_return: 0, gross_exposure: 1, gross_return_index: 1, gross_drawdown: 0, reasons: []};
  return {schema_version: 1, semantics_version: "browser-fixture", study_id: "ff49-industry-mom-project-v1", data_kind: "market_derived_portfolio_returns",
    asset_kind: "industry_portfolio", return_semantics: "monthly_total_return_decimal", source_id: "kenneth-french-49-industry-monthly-value-weighted",
    research_scope: "project_modification", reserved_evaluated: false, human_judgment: null, config: {fixture: limitation}, config_digest: h, input_digest: panel,
    source: {archive_sha256: h, csv_sha256: h, download_url: "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/49_Industry_Portfolios_CSV.zip",
      section: "Average Value Weighted Returns -- Monthly", source_contract: "kenneth-french-49-value-weighted-monthly-v1", header_preamble: [limitation]},
    status: "evaluated", warnings: [limitation], summary: [{...metric, strategy_id: "same_sample_equal_weight_long_only"}, {...metric, strategy_id: "momentum"}],
    months: [{month: "2010-01", as_of: "2009-12-31", status: "evaluated", formation_months: ["2009-01"], skipped_month: "2009-12", eligible_assets: ["Food"], exclusions: [],
      signals: [{asset: "Food", momentum: 0, mom_group: 3}], labels: [{asset: "Food", return_value: null, reasons: ["fixture_missing_label"]}], difference_gross_return: 0,
      strategies: [{...strategy, id: "momentum", net_exposure: 0}, {...strategy, id: "same_sample_equal_weight_long_only", net_exposure: 1}]}],
    paired_comparison: {definition: "different net exposures", months_total: 1, months_evaluated: 1, mean_gross_difference: 0, evaluated_months: ["2010-01"], excluded_months: []}};
}
function source(): IndustryMomSourceDetail {
  return {id: sourceId, digest: h, title: "Browser industry source fixture", note: limitation, created_at: time, data_kind: "market_derived_portfolio_returns",
    asset_kind: "industry_portfolio", market_source_id: "kenneth-french-49-industry-monthly-value-weighted", research_scope: "project_modification",
    archive_sha256: h, method_digest: h, config_digest: h, input_digest: h, panel_digest: panel, result_digest: h, manifest_digest: h, code_digest: h,
    verification_scope: "local_bytes_and_independent_numeric_reference", config_json: "{}", method_json: "{}", source_json: JSON.stringify(result().source), verification_json: "{}",
    human_judgment: null, reserved_evaluated: false};
}
function sourceSummary() {const {config_json: _a, method_json: _b, source_json: _c, verification_json: _d, human_judgment: _e, reserved_evaluated: _f, ...rest} = source(); return rest;}
function detail(id = eid, status: IndustryMomExperimentSummary["status"] = "completed"): IndustryMomExperimentDetail {
  return {experiment: {id, source_id: sourceId, source_digest: h, created_at: time, updated_at: time, status, attempt_count: status === "queued" ? 0 : 1,
    attempt_id: status === "queued" ? null : aid, worker_id: status === "queued" ? null : "fixture-worker", error: null, phase: status === "running" ? "executing" : null,
    input_digest: input, config_digest: h, max_seconds: 60, max_attempts: 3, research_scope: "project_modification"}, source: source(),
    attempts: status === "queued" ? [] : [{id: aid, number: 1, worker_id: "fixture-worker", status, started_at: time, finished_at: time, error: null, result_digest: h, verification_json: null}],
    result_json: status === "completed" ? JSON.stringify(result()) : null, reference_json: status === "completed" ? JSON.stringify({supported: true, passed: true, issues: []}) : null,
    report_markdown: status === "completed" ? `# Browser fixture\n${limitation}` : null, review_target: status === "completed" ? {attempt_id: aid, result_digest: h} : null,
    verification: status === "completed" ? {verified: true, calculation_verified: true, reference_passed: true, source_id: sourceId, source_digest: h, source_input_digest: h,
      source_manifest_digest: h, attempt_id: aid, input_digest: input, result_digest: h, panel_digest: panel, manifest_digest: h, code_digest: h, environment_digest: h,
      config_digest: h, method_digest: h, archive_sha256: h, reserved_evaluated: false, human_review: "pending"} : null};
}
function task(): CaseDetail {
  return {id: cid, digest: h, created_at: time, title: "Browser exact industry case", note: limitation, source_kind: "industry_mom_experiment", source_id: eid, source_digest: h,
    context: {state: "ready_for_review", stop_reason: null, allowed_actions: ["request_human_review"], evidence: [], definitions: [], data_scope: "49 industry portfolios; browser fixture",
      method_scope: "MOM project modification; gross only", provenance: [{label: "attempt_id", value: aid}], results: [{id: aid, kind: "industry_mom_portfolio", digest: h,
        summary: "Synthetic gross output", payload_json: JSON.stringify(result())}], reviews: [], limitations: [limitation]}};
}
function claims(): ClaimDetail {
  return {id: claimsId, digest: h, created_at: time, case_id: cid, case_digest: h, submitted_claims: [], limitations: [limitation], claims: [{id: "metric-reference", kind: "metric",
    attribution: "project_convention", narrative_text: "Browser draft; no superiority claim.", evidence_ids: [], evidence_verifications: [],
    metrics: [{case_id: cid, case_digest: h, result_id: aid, result_digest: h, pointer: "/summary/1/mean_gross_return", value: 0, display: "0", verification: "verified_result_value"}],
    authoritative_display: "0", citation_integrity: "verified", semantic_fidelity: "unverified", status: "draft", actor: "automation"}]};
}
function target(): ClaimReviewTarget {
  const c = task().context;
  return {schema_version: 1, claims_id: claimsId, claims_digest: h, claim_id: "metric-reference", claim_digest: h, case_id: cid, case_digest: h, source_kind: "industry_mom_experiment",
    source_id: eid, source_digest: h, case_context_digest: h, claim: claims().claims[0], evidence: [], definitions: [], results: [{id: aid, kind: "industry_mom_portfolio", digest: h, summary: "Fixture gross"}],
    provenance: c.provenance, data_scope: c.data_scope, method_scope: c.method_scope, case_state: "ready_for_review", stop_reason: null, case_limitations: [limitation],
    original_claim_limitations: [limitation], limitations: [limitation], target_digest: h};
}
type Body = Record<string, unknown>;
async function install(page: Page, options: {empty?: boolean; initial?: "completed" | "running"; lost?: boolean} = {}) {
  const s = {details: new Map<string, IndustryMomExperimentDetail>(), writes: [] as {path: string; body: Body}[], bodies: [] as Body[], effects: new Set<string>(),
    fail: false, unknown: false, stale: false, lose: !!options.lost, previews: [] as Body[]};
  if (options.initial) s.details.set(eid, detail(eid, options.initial));
  const json = (r: Route, value: unknown, status = 200) => r.fulfill({status, contentType: "application/json", body: JSON.stringify(value)});
  await page.route("**/api/**", async r => {
    const req = r.request(), path = new URL(req.url()).pathname, method = req.method();
    if (!["GET", "HEAD"].includes(method)) s.writes.push({path, body: req.postDataJSON()});
    if (path === "/api/industry-mom-sources") return json(r, {items: options.empty ? [] : [sourceSummary()], total: options.empty ? 0 : 1, limit: 20, offset: 0});
    if (path === `/api/industry-mom-sources/${sourceId}`) return json(r, source());
    if (path === "/api/industry-mom-experiments") {
      if (method === "GET") return json(r, {items: [...s.details.values()].map(d => d.experiment), total: s.details.size, limit: 20, offset: 0});
      const b = req.postDataJSON(); s.bodies.push(b); s.effects.add(b.idempotency_key); s.details.set(eid, detail());
      if (s.lose) {s.lose = false; return r.abort();} return json(r, {experiment_id: eid}, 201);
    }
    if (path.startsWith("/api/industry-mom-experiments/")) {
      const parts = path.split("/"), id = parts[3], d = s.details.get(id); if (!d) return json(r, {detail: "Fixture absent"}, 404);
      if (parts[4] === "status") return json(r, {id, status: d.experiment.status, attempt_id: d.experiment.attempt_id, attempt_count: d.experiment.attempt_count,
        phase: d.experiment.phase, change_token: `${id}:${d.experiment.status}`, integrity_checked: false});
      if (["cancel", "retry"].includes(parts[4])) {s.details.set(id, detail(id, parts[4] === "cancel" ? "cancelled" : "queued")); return json(r, {experiment_id: id});}
      if (s.fail) return json(r, {detail: "Synthetic integrity failure"}, 409);
      const response = structuredClone(d); if (s.unknown) response.result_json = JSON.stringify({...result(), data_kind: "unknown_kind"}); return json(r, response);
    }
    if (path === "/api/research-cases/preview") {const {source_kind, source_id, source_digest, context} = task(); if (s.stale) context.results[0].id = "old-other-attempt"; return json(r, {source_kind, source_id, source_digest, context});}
    if (path === "/api/research-cases") {
      if (method === "POST") return json(r, task(), 201); const {context, ...value} = task();
      return json(r, {items: [{...value, state: context.state, stop_reason: null, allowed_actions: context.allowed_actions}], total: 1, limit: 20, offset: 0});
    }
    if (path === `/api/research-cases/${cid}`) return json(r, task());
    if (path === "/api/research-tool-sessions") return json(r, {items: [], total: 0, limit: 20, offset: 0});
    if (path === "/api/research-claims/preview") {s.previews.push(req.postDataJSON()); const {case_id, case_digest, claims: items, limitations} = claims(); return json(r, {case_id, case_digest, claims: items, limitations});}
    if (path === "/api/research-claims") return method === "POST" ? json(r, claims(), 201) : json(r, {items: [], total: 0, limit: 20, offset: 0});
    if (path === `/api/claim-review-targets/${claimsId}`) return json(r, target());
    if (path === "/api/claim-reviews/status") return json(r, {schema_version: 1, target: target(), records: 0, human_records: 0, automation_records: 0,
      superseded_records: 0, active_human_review_ids: [], active_automation_review_ids: [], human_declared_status: "pending", unknown_dimensions: [], semantic_quality_score: null,
      original_result_approval: false, software_verified_semantic_truth: false, limitations: [limitation]});
    if (path === "/api/claim-reviews") return json(r, {items: [], total: 0, limit: 20, offset: 0});
    if (!["GET", "HEAD"].includes(method)) return r.abort(); return r.continue();
  }); return s;
}
async function open(page: Page) {await page.goto("/"); await page.getByRole("button", {name: "研究执行", exact: true}).click(); await expect(page.getByRole("region", {name: "行业组合 MOM 入口", exact: true})).toBeVisible();}
async function choose(page: Page) {
  const region = page.getByRole("region", {name: "行业组合 MOM 入口", exact: true}); await region.getByText("选择来源并运行固定方法", {exact: true}).click();
  await region.getByLabel("行业 MOM 来源", {exact: true}).selectOption(sourceId); await expect(region.getByText(/输入 2009-01 至 2011-12/)).toBeVisible();
  await region.getByLabel("我已查看此次固定来源、方法和研究范围", {exact: true}).check(); return region;
}
const output = (page: Page) => page.getByRole("region", {name: "行业 MOM 实际输出", exact: true});

test("no sources means no fallback and no mutation", async ({page}) => {
  const s = await install(page, {empty: true}); await open(page); await expect(page.getByText(/尚无注册的行业来源/)).toBeVisible();
  await expect(page.getByRole("button", {name: "运行行业 MOM", exact: true})).toBeDisabled(); expect(s.writes).toEqual([]);
});
test("source gross output exact Case claims and five dimensions stay unapproved", async ({page}) => {
  const s = await install(page); await open(page); const region = await choose(page); await region.getByRole("button", {name: "运行行业 MOM", exact: true}).click();
  await expect(output(page)).toBeVisible(); await expect(output(page).getByRole("table", {name: "行业 MOM 摘要指标"})).toContainText("0.000000%");
  await output(page).getByText("检查本月信号、冻结权重和持有标签", {exact: true}).click(); await expect(output(page).getByText("fixture_missing_label", {exact: true})).toBeVisible();
  await expect(page.getByRole("link", {name: "下载本次行业技术报告", exact: true})).toHaveAttribute("href", new RegExp(`expected_attempt_id=${aid}&expected_result_digest=${h}`));
  await page.getByRole("button", {name: "关联此结果并进入研究审核", exact: true}).click(); const caseRegion = page.getByRole("region", {name: "研究任务详情", exact: true});
  await expect(caseRegion).toHaveAttribute("data-case-id", cid); await caseRegion.getByText("带实际数值引用的结论草稿", {exact: true}).click();
  await expect(caseRegion.getByLabel("指标 JSON pointer", {exact: true})).toHaveValue("/summary/1/mean_gross_return");
  await caseRegion.getByLabel("待审核的解释文字", {exact: true}).fill("Browser draft; no superiority claim."); await caseRegion.getByRole("button", {name: "核对实际指标引用", exact: true}).click();
  await caseRegion.getByRole("button", {name: "保存已核对的结论草稿", exact: true}).click(); const review = page.getByRole("region", {name: "精确结论审核", exact: true});
  await expect(review).toContainText("人工 0 条，自动化 0 条"); await review.getByText("填写本版本的五维判断", {exact: true}).click();
  await expect(review.getByLabel("结论审核声明来源", {exact: true})).toHaveValue(""); for (const label of labels) await expect(review.getByLabel(`结论${label}判断`, {exact: true})).toHaveValue("");
  await expect(review.getByRole("button", {name: "预检本版本结论审核", exact: true})).toBeDisabled();
  const req = s.previews[0] as {claims: {metric_references: Body[]}[]}; expect(req.claims[0].metric_references[0]).toMatchObject({result_id: aid, result_digest: h, pointer: "/summary/1/mean_gross_return"});
  expect(s.writes.map(w => w.path)).toEqual(["/api/industry-mom-experiments", "/api/research-cases", "/api/research-claims/preview", "/api/research-claims"]);
});
test("response loss replays identical persisted request after reload with one fixture effect", async ({page}) => {
  const s = await install(page, {lost: true}); await open(page); const region = await choose(page); await region.getByRole("button", {name: "运行行业 MOM", exact: true}).click();
  await expect(page.getByRole("button", {name: "安全重试行业 MOM 实验提交", exact: true})).toBeVisible(); await page.reload(); await page.getByRole("button", {name: "研究执行", exact: true}).click();
  await page.getByRole("button", {name: "安全重试行业 MOM 实验提交", exact: true}).click(); await expect(output(page)).toBeVisible(); expect(s.bodies).toHaveLength(2); expect(s.bodies[0]).toEqual(s.bodies[1]); expect(s.effects.size).toBe(1);
});
test("durable storage failure blocks unrecorded POST", async ({page}) => {
  const s = await install(page); await page.addInitScript(() => {const original = Storage.prototype.setItem; Storage.prototype.setItem = function(key, value) {if (key.includes("industry-mom-create")) throw new Error("Synthetic storage failure"); return original.call(this, key, value);};});
  await open(page); const region = await choose(page); await region.getByRole("button", {name: "运行行业 MOM", exact: true}).click(); await expect(region.getByRole("alert").first()).toBeVisible(); expect(s.bodies).toHaveLength(0);
});
test("status marker cannot release output after full detail fails", async ({page}) => {
  const s = await install(page, {initial: "completed"}); await open(page); await expect(output(page)).toBeVisible(); s.fail = true;
  await page.getByRole("button", {name: "完整核验并刷新行业结果", exact: true}).click(); await expect(page.getByRole("alert").filter({hasText: "完整结果核验失败"})).toBeVisible();
  await expect(output(page)).toHaveCount(0); await expect(page.getByRole("link", {name: "下载本次行业技术报告", exact: true})).toHaveCount(0); expect(s.writes).toEqual([]);
});
test("unknown nested kind refuses fixture and daily metric presentation", async ({page}) => {
  const s = await install(page, {initial: "completed"}); s.unknown = true; await open(page); await expect(page.getByRole("alert").filter({hasText: "行业结果类型或指标合同不匹配"})).toBeVisible(); await expect(output(page)).toHaveCount(0); expect(s.writes).toEqual([]);
});
test("changed preview attempt refuses Case POST", async ({page}) => {
  const s = await install(page, {initial: "completed"}); s.stale = true; await open(page); await expect(output(page)).toBeVisible(); await page.getByRole("button", {name: "关联此结果并进入研究审核", exact: true}).click();
  await expect(page.getByRole("alert").filter({hasText: "准确尝试"})).toBeVisible(); expect(s.writes).toEqual([]);
});
test("cancel retry both freeze current attempt and never create a replacement source", async ({page}) => {
  const s = await install(page, {initial: "running"}); await open(page); await page.getByRole("button", {name: "取消行业 MOM 实验", exact: true}).click(); await page.getByRole("button", {name: "重试行业 MOM 实验", exact: true}).click();
  await expect(page.getByRole("region", {name: "行业 MOM 实验详情", exact: true})).toContainText("流程状态：排队"); expect(s.writes).toHaveLength(2); for (const w of s.writes) expect(w.body.expected_attempt_id).toBe(aid); expect(s.bodies).toHaveLength(0);
});
test("390px source and detailed gross fields stay readable without writes", async ({page}) => {
  const s = await install(page, {initial: "completed"}); await page.setViewportSize({width: 390, height: 1100}); await open(page); await expect(output(page)).toBeVisible(); await choose(page);
  await page.getByText("行业任务、来源和全部尝试身份", {exact: true}).click(); await output(page).getByText("检查本月信号、冻结权重和持有标签", {exact: true}).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true); expect(s.writes).toEqual([]);
});

test("switching workspace never resumes a pending request from the previous identity", async ({page}) => {
  const s = await install(page, {lost: true}); await open(page); const region = await choose(page); await region.getByRole("button", {name: "运行行业 MOM", exact: true}).click();
  await expect(page.getByRole("button", {name: "安全重试行业 MOM 实验提交", exact: true})).toBeVisible();
  const health = await (await page.request.get("/api/health")).json(); health.workspace_id = "d".repeat(64);
  let changed = 0;
  await page.route("**/api/health", async r => {changed++; await r.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(health)});});
  await page.getByRole("button", {name: "刷新工作台", exact: true}).click(); await expect.poll(() => changed).toBeGreaterThan(0);
  await expect(page.getByRole("button", {name: "安全重试行业 MOM 实验提交", exact: true})).toHaveCount(0);
  expect(s.bodies).toHaveLength(1); expect(s.effects.size).toBe(1);
});

test("delayed previous experiment detail cannot overwrite the newly selected experiment", async ({page}) => {
  const s = await install(page, {initial: "completed"}), second = "industry-new-selection-fixture"; s.details.set(second, detail(second));
  let arrived!: () => void, release!: () => void;
  const started = new Promise<void>(resolve => {arrived = resolve;}), gate = new Promise<void>(resolve => {release = resolve;});
  await page.route(`**/api/industry-mom-experiments/${eid}`, async r => {arrived(); await gate; await r.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(detail())});});
  await open(page); await started; await page.getByRole("button", {name: `查看行业 MOM 实验 ${second}`, exact: true}).click();
  const region = page.getByRole("region", {name: "行业 MOM 实验详情", exact: true}); await expect(region).toHaveAttribute("data-industry-experiment-id", second); await expect(output(page)).toBeVisible();
  release(); await page.getByRole("button", {name: "完整核验并刷新行业结果", exact: true}).click(); await expect(region).toHaveAttribute("data-industry-experiment-id", second);
  expect(s.writes).toEqual([]);
});
