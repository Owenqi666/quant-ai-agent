import type { JsonObject, Review, Run } from "./domain";

export const sourceNames: Record<string, string> = { human: "人工填写（声明）", automation: "自动化／代理审核", imported: "导入记录", legacy_unknown: "历史来源未知" };
export const issueStates: Record<string, string> = { open: "待处理", proposed: "已提出修改", awaiting_review: "待复核", resolved: "已解决", deferred: "暂不处理" };
export const dispositions: Record<string, string> = { implementation_fix: "同条件实现修复", evidence_update: "补充／修订论文证据", hypothesis_change: "修改研究假设", data_change: "更换数据版本", accept_limitation: "接受当前限制" };
export interface IssueEvent {
  id: string; issue_id: string; created_at: string; state: string; disposition: string; note: string; source: string;
  revision_id: string | null; target_run_id: string | null; check_id: string | null;
  target_attempt_id?: string | null; target_result_digest?: string | null; resolution_review_id?: string | null;
}
export interface Issue {
  id: string; review_id: string; created_at: string; research_id: string; candidate_id: string;
  source: string; state: string; latest_event_id: string; events: IssueEvent[]; original_review?: Review | null;
}
export interface Ratio { numerator: number; denominator: number; rate: number | null; passed_ids: string[]; eligible_ids: string[] }
export interface FeedbackSummary {
  created_at: string; filters: JsonObject; input_digest: string; inputs: JsonObject;
  skipped_attempts: JsonObject[]; attempt_timings: JsonObject[]; limitations: string[];
  metrics: {
    metrics_version: string; output_count: number; eligible_output_count: number;
    independent_run_count: number; attempt_count: number;
    structured_assessments?: { human_output_coverage: Ratio;
      records: { review_id: string; output_id: string; source: string; semantic_status: string; timing_recorded: boolean; recorded_active_seconds: number | null }[];
      human_active_time: { observed_person_seconds: number | null; by_declared_reviewer: { declared_reviewer: string; union_active_seconds: number; recorded_intervals: number; merged_intervals: number }[] }; scope: string };
    human_review_coverage: Ratio; any_review_coverage: Ratio; issue_closure: Ratio;
    human_issue_closure: Ratio; same_condition_fix_verification: Ratio;
    review_rows_by_source: Record<string, number>; issue_states: Record<string, number>;
    [key: string]: unknown;
  };
}
export type CatalogResource = "researches" | "runs" | "reviews" | "regression-cases" | "regression-checks" | "issues";
export const resourceNames: Record<CatalogResource, string> = { researches: "研究", runs: "实验", reviews: "审核", "regression-cases": "回归案例", "regression-checks": "回归检查", issues: "问题" };
export interface CatalogPage<T> { items: T[]; next_cursor: number; has_more: boolean; high_watermark: number; total_records: number; filters: JsonObject; scope: string }
export interface CatalogFilters { review_id?: string; research_id: string; dataset_id: string; candidate_id: string; status: string; category: string; source: string }
export const emptyCatalogFilters = (): CatalogFilters => ({ research_id: "", dataset_id: "", candidate_id: "", status: "", category: "", source: "" });
export function catalogPath(resource: CatalogResource, filters: Partial<CatalogFilters> = {}, after = 0, through?: number) {
  const query = new URLSearchParams({ after: String(after), limit: "50" });
  if (through !== undefined) query.set("through", String(through));
  for (const [key, value] of Object.entries(filters)) {
    if (!value?.trim()) continue;
    if (key === "review_id" && resource !== "issues") continue;
    if (key === "status" && !["runs", "issues", "regression-checks"].includes(resource)) continue;
    if (["category", "source"].includes(key) && !["reviews", "issues"].includes(resource)) continue;
    query.set(key, value.trim());
  }
  return `/catalog/${resource}?${query}`;
}
export function validateDecision(state: string, disposition: string, note: string, revision: string, run: string, check: string, resolutionReview = "") {
  if (!note.trim()) throw new Error("请填写本次处理依据；系统不会自动生成解决结论。");
  if (state === "resolved" && (!revision.trim() || !run.trim())) throw new Error("解决问题必须明确关联修订和目标实验。");
  if (state === "resolved" && disposition === "implementation_fix" && !check.trim())
    throw new Error("同条件实现修复需要目标结果上的回归检查，且原审核批准的案例通过；请选择对应检查。");
  if (state === "resolved" && !resolutionReview.trim()) throw new Error("关闭问题必须选择目标新实验上的接受复审；原审核不能替代复审。");
}

/** Presence helps navigation; the server still verifies every frozen identity. */
export function matchesResolutionReview(review: Review, issue: Issue, run: Run | null, requireAccepted = true): boolean {
  if (!run || !["completed", "failed"].includes(run.status) || run.verification?.verified !== true) return false;
  const target = run.review_targets?.find((row) => row.candidate_id === issue.candidate_id);
  return !!target && review.id !== issue.review_id && run.id !== issue.original_review?.run_id
    && review.run_id === run.id && review.revision_id === run.revision_id
    && review.candidate_id === issue.candidate_id && review.attempt_id === target.attempt_id
    && review.result_digest === target.result_digest && (!requireAccepted || review.verdict === "accepted");
}
