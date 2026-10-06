import type { MonthlyConfig, MonthlyDetail, MonthlySummary } from "./generated/api-contract";

export const monthlyStrategyNames = { momentum: "MOM 基线", mom_id: "MOM × ID 对照" };
export const monthlyStateNames: Record<string, string> = {
  queued: "排队", running: "运行中", cancelling: "正在取消", completed: "流程完成",
  failed: "运行失败", cancelled: "已取消", interrupted: "运行中断",
  evaluated: "已计算", partial: "部分可用", not_evaluable: "无法评估", unavailable: "不可用", blocked: "规则未决",
};
export const monthlyActive = (status: string) => ["queued", "running", "cancelling"].includes(status);
export function monthlyNumber(value: number | null | undefined, percent = false): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未提供";
  return percent ? `${(value * 100).toPrecision(7)}%` : value.toPrecision(7);
}
export function monthlyConfigError(config: MonthlyConfig | null): string {
  if (!config) return "正在读取月度实验默认配置。";
  const month = /^(19[0-9]{2}|20[0-9]{2})-(0[1-9]|1[0-2])$/;
  if (![config.start_month, config.end_month].every(value => month.test(value) && value >= "1905-01" && value <= "2099-12"))
    return "开始与结束月份须为 1905-01 至 2099-12。";
  const index = (value: string) => Number(value.slice(0, 4)) * 12 + Number(value.slice(5));
  const count = index(config.end_month) - index(config.start_month) + 1;
  if (count < 1 || count > 24) return "月度范围须为连续 1–24 个月，结束不早于开始。";
  if (!Number.isFinite(config.cost_bps) || config.cost_bps < 0 || config.cost_bps > 100) return "代理成本须为 0–100 bps 的有限数值。";
  if (!Number.isInteger(config.min_assets) || config.min_assets < 9 || config.min_assets > 128) return "形成样本下限须为 9–128 的整数。";
  return "";
}
/** A completed pipeline alone does not establish a reviewable result. */
export function monthlyReviewTarget(detail: MonthlyDetail | null) {
  if (!detail || detail.experiment.status !== "completed" || !detail.verification.verified || !detail.result || !detail.review_target
      || detail.review_target.attempt_id !== detail.experiment.attempt_id
      || detail.review_target.result_digest !== detail.verification.result_digest) return null;
  return detail.review_target;
}
export function monthlyCanRetry(experiment: MonthlySummary) {
  return !!experiment.attempt_id && experiment.attempt_count < 3 && ["failed", "interrupted", "cancelled"].includes(experiment.status);
}
