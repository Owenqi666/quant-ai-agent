import type {IndustryMomExperimentDetail} from "./generated/api-contract";

/** Presentation of the fixed industry-return domain. No financial metric is calculated here. */
export const industryStrategyNames = {
  momentum: "行业 MOM · 多空组合",
  same_sample_equal_weight_long_only: "同形成样本 · 行业等权多头",
} as const;
export type IndustryStrategyId = keyof typeof industryStrategyNames;
export type IndustryMomMetric = {
  strategy_id: IndustryStrategyId; months_total: number; months_evaluated: number;
  mean_gross_return: number | null; annualized_sample_volatility: number | null;
  annualized_mean_over_volatility_zero_rf: number | null; terminal_gross_return_index: number | null;
  max_gross_drawdown: number | null; cumulative_complete: boolean; excluded_months: string[];
};
export type IndustryMomStrategy = {
  id: IndustryStrategyId; status: "evaluated" | "unavailable";
  weights: {asset: string; weight: number}[]; gross_return: number | null;
  gross_exposure: number | null; net_exposure: number | null;
  gross_return_index: number | null; gross_drawdown: number | null; reasons: string[];
};
export type IndustryMomPeriod = {
  month: string; as_of: string; status: "evaluated" | "partial" | "unavailable";
  formation_months: string[]; skipped_month: string; eligible_assets: string[];
  exclusions: {asset: string; reasons: string[]}[];
  signals: {asset: string; momentum: number; mom_group: number}[];
  labels: {asset: string; return_value: number | null; reasons: string[]}[];
  strategies: IndustryMomStrategy[]; difference_gross_return: number | null;
};
export type IndustryMomResult = {
  schema_version: 1; semantics_version: string; study_id: string;
  data_kind: "market_derived_portfolio_returns"; source_id: "kenneth-french-49-industry-monthly-value-weighted";
  asset_kind: "industry_portfolio"; return_semantics: "monthly_total_return_decimal";
  research_scope: "project_modification"; human_judgment: null; reserved_evaluated: false;
  config: Record<string, unknown>; config_digest: string; input_digest: string;
  source: {archive_sha256: string; csv_sha256: string; download_url: string; section: string; source_contract: string; header_preamble: string[]};
  status: "evaluated" | "partial" | "not_evaluable"; warnings: string[];
  months: IndustryMomPeriod[]; summary: IndustryMomMetric[];
  paired_comparison: {definition: string; months_evaluated: number; months_total: number;
    evaluated_months: string[]; excluded_months: string[]; mean_gross_difference: number | null};
};

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === "string");
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const optionalNumber = (value: unknown) => value === null || finite(value);
const hash = (value: unknown) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const strategy = (value: unknown): value is IndustryStrategyId => value === "momentum" || value === "same_sample_equal_weight_long_only";
const noProxies = (value: Record<string, unknown>) => !Object.keys(value).some(key => /net_return|proxy|turnover|cost|nav/.test(key));
const counts = (value: Record<string, unknown>) => Number.isInteger(value.months_total) && Number.isInteger(value.months_evaluated)
  && Number(value.months_total) >= 1 && Number(value.months_total) <= 24 && Number(value.months_evaluated) >= 0
  && Number(value.months_evaluated) <= Number(value.months_total);

/** The nested JSON is a stored engine result, separate from the HTTP response DTO. */
export function parseIndustryMomResult(raw: string): IndustryMomResult {
  if (typeof raw !== "string" || raw.length > 4 * 1024 * 1024) throw new Error("行业结果超过读取范围或格式不正确。");
  let value: unknown;
  try { value = JSON.parse(raw); } catch { throw new Error("行业结果无法读取，旧输出不会被当作成功结果。"); }
  const invalid = () => { throw new Error("行业结果类型或指标合同不匹配；未采用日度/受控月度结果替代。"); };
  if (!object(value) || value.schema_version !== 1 || typeof value.semantics_version !== "string"
      || value.study_id !== "ff49-industry-mom-project-v1" || value.data_kind !== "market_derived_portfolio_returns"
      || value.source_id !== "kenneth-french-49-industry-monthly-value-weighted" || value.asset_kind !== "industry_portfolio"
      || value.return_semantics !== "monthly_total_return_decimal" || value.research_scope !== "project_modification"
      || value.human_judgment !== null || value.reserved_evaluated !== false
      || !hash(value.input_digest) || !hash(value.config_digest) || !object(value.config)
      || !["evaluated", "partial", "not_evaluable"].includes(String(value.status)) || !strings(value.warnings)) return invalid();
  const source = value.source;
  if (!object(source) || !hash(source.archive_sha256) || !hash(source.csv_sha256)
      || source.download_url !== "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/49_Industry_Portfolios_CSV.zip"
      || source.section !== "Average Value Weighted Returns -- Monthly" || source.source_contract !== "kenneth-french-49-value-weighted-monthly-v1"
      || !strings(source.header_preamble)) return invalid();
  if (!Array.isArray(value.summary) || value.summary.length !== 2 || new Set(value.summary.map(row => object(row) ? row.strategy_id : null)).size !== 2) return invalid();
  for (const row of value.summary) {
    if (!object(row) || !strategy(row.strategy_id) || !counts(row) || !noProxies(row) || typeof row.cumulative_complete !== "boolean"
        || !strings(row.excluded_months) || !["mean_gross_return", "annualized_sample_volatility", "annualized_mean_over_volatility_zero_rf",
          "terminal_gross_return_index", "max_gross_drawdown"].every(key => optionalNumber(row[key]))) return invalid();
  }
  if (!Array.isArray(value.months) || !value.months.length || value.months.length > 24) return invalid();
  let previous = "";
  for (const month of value.months) {
    if (!object(month) || typeof month.month !== "string" || !/^201[01]-(0[1-9]|1[0-2])$/.test(month.month) || month.month <= previous
        || typeof month.as_of !== "string" || !["evaluated", "partial", "unavailable"].includes(String(month.status))
        || !strings(month.formation_months) || typeof month.skipped_month !== "string" || !strings(month.eligible_assets)
        || !optionalNumber(month.difference_gross_return) || !Array.isArray(month.exclusions)
        || !Array.isArray(month.signals) || !Array.isArray(month.labels) || !Array.isArray(month.strategies) || month.strategies.length !== 2) return invalid();
    previous = month.month;
    for (const exclusion of month.exclusions) if (!object(exclusion) || typeof exclusion.asset !== "string" || !strings(exclusion.reasons)) return invalid();
    for (const signal of month.signals) if (!object(signal) || typeof signal.asset !== "string" || !finite(signal.momentum) || !Number.isInteger(signal.mom_group)) return invalid();
    for (const label of month.labels) if (!object(label) || typeof label.asset !== "string" || !optionalNumber(label.return_value) || !strings(label.reasons)) return invalid();
    if (new Set(month.strategies.map(row => object(row) ? row.id : null)).size !== 2) return invalid();
    for (const row of month.strategies) {
      if (!object(row) || !strategy(row.id) || !noProxies(row) || !["evaluated", "unavailable"].includes(String(row.status))
          || !strings(row.reasons) || !Array.isArray(row.weights) || !["gross_return", "gross_exposure", "net_exposure", "gross_return_index", "gross_drawdown"].every(key => optionalNumber(row[key]))) return invalid();
      for (const weight of row.weights) if (!object(weight) || typeof weight.asset !== "string" || !finite(weight.weight)) return invalid();
    }
  }
  const paired = value.paired_comparison;
  if (!object(paired) || typeof paired.definition !== "string" || !counts(paired) || !strings(paired.evaluated_months)
      || !strings(paired.excluded_months) || !optionalNumber(paired.mean_gross_difference)) return invalid();
  return value as IndustryMomResult;
}

export const industryActive = (status: string) => ["queued", "running", "cancelling"].includes(status);
export const industryCanRetry = (experiment: {status: string; attempt_id: string | null; attempt_count: number}) =>
  !!experiment.attempt_id && experiment.attempt_count < 3 && ["failed", "interrupted", "cancelled"].includes(experiment.status);
export const industryStateNames: Record<string, string> = {
  queued: "排队", running: "运行中", cancelling: "正在取消", completed: "计算流程完成",
  failed: "运行失败", cancelled: "已取消", interrupted: "运行中断", evaluated: "已计算", partial: "部分可用", not_evaluable: "无法评估", unavailable: "不可用",
};
export function industryNumber(value: number | null | undefined, percent = false) {
  if (!finite(value)) return "不可用 / 未计算";
  return percent ? `${(value * 100).toPrecision(7)}%` : value.toPrecision(7);
}

/** A status probe does not establish a scientific output or authorize a new Case. */
export function industryVerifiedResult(detail: IndustryMomExperimentDetail | null): IndustryMomResult | null {
  if (!detail || detail.experiment.status !== "completed" || !detail.result_json || !detail.reference_json || !detail.report_markdown) return null;
  const {experiment, source, verification: v, review_target: target} = detail;
  if (!v || !target || v.verified !== true || v.calculation_verified !== true || v.reference_passed !== true
      || !experiment.attempt_id || v.attempt_id !== experiment.attempt_id || target.attempt_id !== v.attempt_id
      || target.result_digest !== v.result_digest || experiment.source_id !== source.id || v.source_id !== source.id
      || experiment.source_digest !== source.digest || v.source_digest !== source.digest
      || v.source_input_digest !== source.input_digest || v.source_manifest_digest !== source.manifest_digest
      || v.input_digest !== experiment.input_digest || v.config_digest !== experiment.config_digest
      || v.config_digest !== source.config_digest || v.method_digest !== source.method_digest
      || v.archive_sha256 !== source.archive_sha256 || v.reserved_evaluated !== false || v.human_review !== "pending") return null;
  const result = parseIndustryMomResult(detail.result_json);
  if (result.input_digest !== v.panel_digest || result.config_digest !== v.config_digest || result.source.archive_sha256 !== v.archive_sha256) return null;
  let reference: unknown;
  try { reference = JSON.parse(detail.reference_json); } catch { throw new Error("独立数值参考无法读取；未展示科学结果。"); }
  if (!object(reference) || reference.supported !== true || reference.passed !== true || !Array.isArray(reference.issues) || reference.issues.length) return null;
  return result;
}

export function industryReportPath(id: string, target: {attempt_id: string; result_digest: string}) {
  return `/api/industry-mom-experiments/${encodeURIComponent(id)}/report?${new URLSearchParams({
    expected_attempt_id: target.attempt_id, expected_result_digest: target.result_digest,
  })}`;
}
