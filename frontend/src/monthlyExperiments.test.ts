import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { MonthlyConfig, MonthlyDetail, MonthlyResult, MonthlySummary } from "./generated/api-contract";
import { monthlyCanRetry, monthlyConfigError, monthlyNumber, monthlyReviewTarget } from "./monthlyExperiments";
import { MonthlyResultView } from "./components/MonthlyExperiments";

const config: MonthlyConfig = { schema_version: 1, start_month: "2025-01", end_month: "2025-06", cost_bps: 0, min_assets: 18 };
describe("monthly experiment presentation contracts", () => {
  it("rejects inverted/oversized month ranges and invalid cost or asset thresholds", () => {
    expect(monthlyConfigError(config)).toBe("");
    expect(monthlyConfigError({ ...config, end_month: "2026-12" })).toBe("");
    for (const change of [{ start_month: "1904-12" }, { end_month: "2100-01" }, { end_month: "2024-12" },
      { end_month: "2027-01" }, { end_month: "2025-13" }, { cost_bps: NaN }, { cost_bps: 101 },
      { min_assets: 8 }, { min_assets: 18.5 }, { min_assets: Infinity }])
      expect(monthlyConfigError({ ...config, ...change })).not.toBe("");
  });
  it("preserves actual zero while absent and nonfinite values never become numbers", () => {
    expect(monthlyNumber(0)).toBe("0.000000");
    expect(monthlyNumber(0, true)).toBe("0.000000%");
    for (const value of [null, undefined, NaN, Infinity]) expect(monthlyNumber(value, true)).toBe("未提供");
    expect(monthlyNumber(-0.25, true)).toBe("-25.00000%");
  });
  it("requires completed verified matching attempt and digest before review/report", () => {
    const value = { experiment: { status: "completed", attempt_id: "attempt" }, result: { status: "blocked" },
      verification: { verified: true, result_digest: "digest" }, review_target: { attempt_id: "attempt", result_digest: "digest" } } as MonthlyDetail;
    expect(monthlyReviewTarget(value)).toEqual(value.review_target);
    expect(monthlyReviewTarget(null)).toBeNull();
    expect(monthlyReviewTarget({ ...value, verification: { ...value.verification, verified: false } })).toBeNull();
    expect(monthlyReviewTarget({ ...value, experiment: { ...value.experiment, status: "running" } })).toBeNull();
    expect(monthlyReviewTarget({ ...value, review_target: { attempt_id: "old", result_digest: "digest" } })).toBeNull();
    expect(monthlyReviewTarget({ ...value, review_target: { attempt_id: "attempt", result_digest: "old" } })).toBeNull();
    expect(monthlyReviewTarget({ ...value, result: null })).toBeNull();
  });
  it("does not offer retry for cancelled-before-start or exhausted attempts", () => {
    const value = { status: "cancelled", attempt_id: "attempt", attempt_count: 1 } as MonthlySummary;
    expect(monthlyCanRetry(value)).toBe(true);
    expect(monthlyCanRetry({ ...value, attempt_id: null })).toBe(false);
    expect(monthlyCanRetry({ ...value, attempt_count: 3 })).toBe(false);
    expect(monthlyCanRetry({ ...value, status: "completed" })).toBe(false);
  });
  it("renders provider numbers, separate denominators and proxy limitations without filling nulls", () => {
    const result = { schema_version: 1, status: "partial", data_kind: "controlled_fixture", source_id: "display-fixture",
      semantics_version: "monthly-portfolio-v1", config, config_digest: "cfg", input_digest: "input", rules: {}, warnings: [], months: [],
      summary: [{ strategy_id: "momentum", months_total: 6, months_evaluated: 2, gross_months: 3, turnover_months: 4,
        mean_gross_return: 0, mean_net_return_proxy: null, volatility_annualized_proxy: null, sharpe_annualized_proxy: null,
        max_drawdown_proxy: null, terminal_nav_proxy: null, mean_turnover_proxy: 0.25, cumulative_complete: false }],
    } as unknown as MonthlyResult;
    const html = renderToStaticMarkup(createElement(MonthlyResultView, { result }));
    expect(html).toContain("3 / 2 / 4；共 6");
    expect(html).toContain("0.000000%");
    expect(html).toContain("未提供");
    expect(html).toContain("不完整，不补接");
    expect(html).toContain("不按未来标签筛掉持仓或重新归一化");
    expect(html).not.toContain("NaN");
  });
});
