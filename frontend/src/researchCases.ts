import type { CaseContext, CaseDetail, CasePreview, CaseResult } from "./generated/api-contract";

export type ResearchCaseSource = CaseDetail["source_kind"] | "industry_mom_experiment";
export const caseSourceNames: Record<ResearchCaseSource, string> = {
  daily_run: "因子实验", monthly_experiment: "受控月度实验", author_study: "作者数据准入",
  industry_mom_experiment: "行业组合 MOM · 真实历史来源",
};

/** Each source has its own collection; an unknown domain must never fall back to a fixture. */
export function caseSourceCollection(kind: string): string {
  if (kind === "author_study") return "/author-studies";
  if (kind === "monthly_experiment") return "/monthly-experiments";
  if (kind === "industry_mom_experiment") return "/industry-mom-experiments";
  if (kind === "daily_run") return "/catalog/runs";
  throw new Error("未知研究来源；没有采用其他数据路径替代。");
}

/** A metric reference uses the stored strategy identity, never an assumed array position. */
export function defaultMetricPointer(result?: {kind: string; payload_json: string}): string {
  if (!result) return "";
  if (result.kind === "author_eligibility") return "/summary/min_selected";
  if (result.kind === "monthly_portfolio") return "/summary/0/months_evaluated";
  if (result.kind === "daily_candidates") return "/0/result/metrics/mean_rank_ic";
  if (result.kind === "industry_mom_portfolio") {
    try {
      const value: unknown = JSON.parse(result.payload_json);
      if (!record(value) || value.data_kind !== "market_derived_portfolio_returns" || !Array.isArray(value.summary)) return "";
      const matches = value.summary.flatMap((row, index) => record(row) && row.strategy_id === "momentum" ? [index] : []);
      return matches.length === 1 ? `/summary/${matches[0]}/mean_gross_return` : "";
    } catch { return ""; }
  }
  return "";
}
export const caseStateNames: Record<CaseContext["state"], string> = {
  ready_for_review: "计算完成，等待评审", data_insufficient: "数据不足，保留阻断结论",
  rules_unresolved: "方法尚未确定", implementation_failed: "实现需要检查",
};
export const caseActionNames: Record<CaseContext["allowed_actions"][number], string> = {
  request_human_review: "交由人工评审", revise_plan: "提出新计划供人工审查",
  resolve_method: "先补齐方法规则", stop_data_insufficient: "因数据不足停止当前尝试",
};
export const caseOriginNames: Record<string, string> = {
  paper: "论文依据", author_code: "作者代码", project: "项目约定", unknown: "来源未确认",
  paper_original: "原文定义", user_modification: "用户修改", model_conjecture: "模型推测",
  project_convention: "项目约定", unresolved: "尚未确定",
};

export function caseCreateBody(preview: CasePreview | null, title: string, note: string) {
  if (!preview || !title.trim() || title.length > 200 || note.length > 4000) throw new Error("请先核验来源，并填写任务名称。");
  return {title: title.trim(), note, source_kind: preview.source_kind, source_id: preview.source_id, source_digest: preview.source_digest};
}

export function caseProposalBody(detail: CaseDetail, action: CaseContext["allowed_actions"][number], rationale: string, evidenceIds: string[]) {
  if (!detail.context.allowed_actions.includes(action)) throw new Error("此研究状态不允许该下一步。");
  if (!rationale.trim() || rationale.length > 2000) throw new Error("请填写 1–2000 字建议依据。");
  const allowed = new Set(detail.context.evidence.map(item => item.id));
  if (new Set(evidenceIds).size !== evidenceIds.length || evidenceIds.some(id => !allowed.has(id))) throw new Error("建议只能引用当前任务已有的证据。");
  return {tool: "propose_next_action", arguments: {action, rationale, evidence_ids: evidenceIds}};
}

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
/** Display saved values only; never recompute financial metrics in the browser. */
export function resultHighlights(result: CaseResult): {key: string; value: string}[] {
  let payload: unknown;
  try {payload = JSON.parse(result.payload_json);} catch {return [];}
  const rows: {key: string; value: string}[] = [];
  function scalars(value: unknown, prefix: string) {
    if (!record(value)) return;
    Object.entries(value).forEach(([key, item]) => {
      if (rows.length >= 24) return;
      if (typeof item === "number" && Number.isFinite(item) || item === null || typeof item === "boolean" || typeof item === "string" && item.length <= 100) {
        rows.push({key: prefix + key, value: item === null ? "未计算 / 不可用" : String(item)});
      }
    });
  }
  if (record(payload) && Array.isArray(payload.summary)) payload.summary.forEach((value, i) => scalars(value, `${record(value) ? value.strategy_id ?? i + 1 : i + 1} · `));
  else if (record(payload) && record(payload.summary)) scalars(payload.summary, "");
  else {
    const candidates = Array.isArray(payload) ? payload : record(payload) && Array.isArray(payload.candidates) ? payload.candidates : [];
    candidates.forEach(value => {if (record(value) && record(value.result)) scalars(value.result.metrics, `${value.id ?? "候选"} · `);});
    if (!rows.length) scalars(payload, "");
  }
  return rows;
}
