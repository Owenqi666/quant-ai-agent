import {describe, expect, it} from "vitest";
import {createElement} from "react";
import {renderToStaticMarkup} from "react-dom/server";
import type {IndustryMomExperimentDetail} from "./generated/api-contract";
import {industryCanRetry, industryNumber, industryReportPath, industryVerifiedResult, parseIndustryMomResult} from "./industryMomExperiments";
import {caseSourceCollection, defaultMetricPointer} from "./researchCases";
import {IndustryMomResultView} from "./components/IndustryMomExperiments";

const hash = "a".repeat(64), input = "b".repeat(64), panel = "c".repeat(64);
/** Synthetic UI contract values. This fixture is not an official download or evaluated strategy. */
function resultFixture() {
  const metric = {months_total: 1, months_evaluated: 1, mean_gross_return: 0, annualized_sample_volatility: null,
    annualized_mean_over_volatility_zero_rf: null, terminal_gross_return_index: 1, max_gross_drawdown: 0, cumulative_complete: true, excluded_months: []};
  const strategy = {status: "evaluated", weights: [{asset: "Food", weight: 0.5}], gross_return: 0,
    gross_exposure: 1, gross_return_index: 1, gross_drawdown: 0, reasons: []};
  return {schema_version: 1, semantics_version: "industry-mom-fixture", study_id: "ff49-industry-mom-project-v1",
    data_kind: "market_derived_portfolio_returns", asset_kind: "industry_portfolio", return_semantics: "monthly_total_return_decimal",
    source_id: "kenneth-french-49-industry-monthly-value-weighted", research_scope: "project_modification", reserved_evaluated: false, human_judgment: null,
    config: {fixture_scope: "synthetic UI fixture only"}, config_digest: hash, input_digest: panel,
    source: {archive_sha256: hash, csv_sha256: hash, download_url: "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/49_Industry_Portfolios_CSV.zip",
      section: "Average Value Weighted Returns -- Monthly", source_contract: "kenneth-french-49-value-weighted-monthly-v1", header_preamble: ["UI fixture, not real market input"]},
    status: "evaluated", warnings: ["Synthetic display fixture; not an actual download or market result."],
    summary: [{...metric, strategy_id: "same_sample_equal_weight_long_only"}, {...metric, strategy_id: "momentum"}],
    months: [{month: "2010-01", as_of: "2009-12-31", status: "evaluated", formation_months: ["2009-01"], skipped_month: "2009-12",
      eligible_assets: ["Food"], exclusions: [], signals: [{asset: "Food", momentum: 0, mom_group: 3}],
      labels: [{asset: "Food", return_value: null, reasons: ["fixture_missing_label"]}], difference_gross_return: 0,
      strategies: [{...strategy, id: "momentum", net_exposure: 0}, {...strategy, id: "same_sample_equal_weight_long_only", net_exposure: 1}]}],
    paired_comparison: {definition: "different net exposures", months_total: 1, months_evaluated: 1, mean_gross_difference: 0, evaluated_months: ["2010-01"], excluded_months: []}};
}

function detailFixture() {
  return {experiment: {status: "completed", attempt_id: "attempt", source_id: "source", source_digest: hash, input_digest: input, config_digest: hash},
    source: {id: "source", digest: hash, input_digest: hash, manifest_digest: hash, config_digest: hash, method_digest: hash, archive_sha256: hash},
    result_json: JSON.stringify(resultFixture()), reference_json: JSON.stringify({supported: true, passed: true, issues: []}), report_markdown: "Fixture report",
    verification: {verified: true, calculation_verified: true, reference_passed: true, source_id: "source", source_digest: hash,
      source_input_digest: hash, source_manifest_digest: hash, input_digest: input, result_digest: hash, panel_digest: panel, attempt_id: "attempt",
      config_digest: hash, method_digest: hash, archive_sha256: hash, reserved_evaluated: false, human_review: "pending"},
    review_target: {attempt_id: "attempt", result_digest: hash}} as IndustryMomExperimentDetail;
}

describe("industry MOM domain and exact output presentation", () => {
  it("never routes an unknown domain to the controlled fixture or daily IC path", () => {
    expect(caseSourceCollection("industry_mom_experiment")).toBe("/industry-mom-experiments");
    expect(() => caseSourceCollection("unknown_source")).toThrow();
    expect(defaultMetricPointer({kind: "industry_mom_portfolio", payload_json: JSON.stringify(resultFixture())})).toBe("/summary/1/mean_gross_return");
    expect(defaultMetricPointer({kind: "unknown_kind", payload_json: "[]"})).toBe("");
    expect(defaultMetricPointer({kind: "industry_mom_portfolio", payload_json: "{"})).toBe("");
  });
  it("rejects fixture/stock domains, future months, proxy strategy fields and invented human judgments", () => {
    for (const change of [{data_kind: "controlled_fixture"}, {asset_kind: "individual_stock"}, {reserved_evaluated: true}, {human_judgment: "passed"}])
      expect(() => parseIndustryMomResult(JSON.stringify({...resultFixture(), ...change}))).toThrow();
    const proxy = resultFixture(); proxy.summary[0] = {...proxy.summary[0], mean_net_return_proxy: 0} as typeof proxy.summary[0];
    expect(() => parseIndustryMomResult(JSON.stringify(proxy))).toThrow();
    const future = resultFixture(); future.months[0].month = "2012-01";
    expect(() => parseIndustryMomResult(JSON.stringify(future))).toThrow();
  });
  it("requires complete detail and matching source/attempt/result/panel/config identities", () => {
    const detail = detailFixture(); expect(industryVerifiedResult(detail)?.summary).toHaveLength(2);
    for (const bad of [null, {...detail, result_json: null}, {...detail, reference_json: null},
      {...detail, experiment: {...detail.experiment, status: "running"}},
      {...detail, review_target: {...detail.review_target!, attempt_id: "old"}},
      {...detail, verification: {...detail.verification!, source_digest: input}},
      {...detail, verification: {...detail.verification!, input_digest: hash}},
      {...detail, verification: {...detail.verification!, panel_digest: input}},
      {...detail, verification: {...detail.verification!, reference_passed: false}}])
      expect(industryVerifiedResult(bad as IndustryMomExperimentDetail | null)).toBeNull();
  });
  it("binds technical downloads to the precise attempt and digest", () => {
    const path = industryReportPath("id/path", {attempt_id: "attempt", result_digest: hash});
    expect(path).toContain("id%2Fpath/report?"); expect(path).toContain("expected_attempt_id=attempt"); expect(path).toContain(`expected_result_digest=${hash}`);
  });
  it("renders only saved gross fields with separate denominators, actual zero and missing reasons", () => {
    const result = parseIndustryMomResult(JSON.stringify(resultFixture()));
    const html = renderToStaticMarkup(createElement(IndustryMomResultView, {result}));
    expect(html).toContain("同形成样本"); expect(html).toContain("1 / 1"); expect(html).toContain("0.000000%");
    expect(html).toContain("不可用 / 未计算"); expect(html).toContain("fixture_missing_label"); expect(html).toContain("净敞口不同");
    expect(html).not.toContain("MOM × ID"); expect(html).not.toContain("net_return_proxy"); expect(html).not.toContain("NaN");
  });
  it("offers retry only for an existing unsuccessful attempt within the bound", () => {
    expect(industryCanRetry({status: "failed", attempt_id: "attempt", attempt_count: 1})).toBe(true);
    for (const experiment of [{status: "cancelled", attempt_id: null, attempt_count: 0}, {status: "failed", attempt_id: "attempt", attempt_count: 3}, {status: "completed", attempt_id: "attempt", attempt_count: 1}])
      expect(industryCanRetry(experiment)).toBe(false);
    expect(industryNumber(0, true)).toBe("0.000000%"); expect(industryNumber(null)).toBe("不可用 / 未计算"); expect(industryNumber(NaN)).toBe("不可用 / 未计算");
  });
});
