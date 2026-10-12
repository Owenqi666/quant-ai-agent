import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Run } from "./domain";
import type { CandidateSeriesResponse } from "./generated/api-contract";
import { LanguageProvider } from "./i18n";
import { CandidateSeriesView } from "./components/CandidateCharts";
import ResearchReviewForm, { CandidateComparison } from "./components/ResearchReviewForm";
import ReviewEvidence from "./components/ReviewEvidence";
import { initialReviewDraft, loadReviewDraft, requireReviewVerdict, saveReviewDraft } from "./reviewRecovery";

const run: Run = {
  id: "run", research_id: "research", revision_id: "frozen-v1", status: "completed", mode: "normalized_fixed", created_at: "", attempt_count: 1,
  verification: { verified: true }, review_targets: [{ candidate_id: "alpha", attempt_id: "attempt", result_digest: "digest" }],
  state: {
    task: { candidates: [{ id: "alpha", hypothesis_id: "hypothesis", expression: "close / open", origin: "paper_original", changes: [] }],
      hypotheses: [{ id: "hypothesis", claim: "原始假设，不应翻译", attribution: "paper_original", economic_mechanism: "Source explanation stays unchanged", mechanism_attribution: "paper_original", signal_direction: "higher long", required_fields: ["close", "open"], assumptions: [], evidence_ids: ["quote"] }],
      evidence: [{ id: "quote", page: 2, quote: "原文 original quotation" }] },
    candidates: [{ id: "alpha", hypothesis_id: "hypothesis", expression: "close / open", origin: "paper_original", changes: [], status: "evaluated",
      result: { expression: "close / open", metrics: { mean_rank_ic: 0, mean_gross_return: 0.02, factor_coverage: 0.5, evaluated_days: 2 }, market_metadata: { data_kind: "synthetic", version: "actual-fixture" } } }],
  },
};
const series: CandidateSeriesResponse = {
  schema_version: 1, run_id: "run", research_id: "research", revision_id: "frozen-v1", revision_digest: "revision-digest",
  attempt_id: "attempt", candidate_id: "alpha", result_digest: "digest", integrity: "verified", status: "evaluated", reason: null,
  source: { artifact_id: "artifact", artifact_name: "result.json", artifact_sha256: "artifact-digest", original_sha256: "original", result_sha256: "result", json_pointer: "/daily" },
  data: { dataset_id: "data", dataset_sha256: "data-digest", version: "actual-fixture", data_kind: "synthetic", universe_size: 4 },
  evaluation: { split: "validation", start: "2026-01-01", end: "2026-01-06", min_assets: 2 },
  summary: { sum_gross_return: 0.0379, mean_gross_return: 0.02, mean_rank_ic: 0, rank_ic_days: 1, evaluated_days: 2, skipped_days: 1, purged_days: 0, factor_coverage: 0.5 },
  points: [
    { signal_date: "2026-01-01", entry_date: "2026-01-02", exit_date: "2026-01-03", status: "evaluated", reason: null, gross_return: 0, cumulative_gross_return: 0, rank_ic: 0, rank_ic_state: "defined", available_assets: 4, coverage: 1 },
    { signal_date: "2026-01-02", entry_date: "2026-01-03", exit_date: "2026-01-04", status: "skipped", reason: "constant_factor", gross_return: null, cumulative_gross_return: null, rank_ic: null, rank_ic_state: "not_evaluated", available_assets: 4, coverage: 1 },
    { signal_date: "2026-01-03", entry_date: "2026-01-04", exit_date: "2026-01-05", status: "evaluated", reason: null, gross_return: 0.02, cumulative_gross_return: 0.0379, rank_ic: null, rank_ic_state: "constant_forward_returns", available_assets: 2, coverage: 0.5 },
  ], limitations: ["Original limitation text"],
};
function english(element: ReactElement) {
  vi.stubGlobal("localStorage", { getItem: () => "en" });
  return renderToStaticMarkup(createElement(LanguageProvider, null, element));
}
afterEach(() => vi.unstubAllGlobals());

describe("live result review presents actual frozen outputs", () => {
  it("compares verified values but suppresses the same retained numbers when integrity changes", () => {
    const props = { run, candidates: run.state!.candidates!, artifacts: [], selected: "alpha", disabled: false, onSelect: () => {} };
    const trusted = renderToStaticMarkup(createElement(CandidateComparison, props));
    expect(trusted).toContain("2.00%"); expect(trusted).toContain("0.000000");
    expect(trusted).toContain('aria-pressed="true"');
    const unverified = renderToStaticMarkup(createElement(CandidateComparison, { ...props, run: { ...run, verification: { verified: false } } }));
    expect(unverified).not.toContain("2.00%"); expect(unverified).not.toContain("0.000000");
    expect(unverified).toContain("未计算");
  });
  it("translates interface labels without translating evidence, hypotheses, expressions or result identities", () => {
    const html = english(createElement(ReviewEvidence, { run, candidateId: "alpha", artifacts: [] }));
    for (const phrase of ["Current candidate output", "Computed output", "Declared expression", "Executed expression", "Frozen evidence and implementation",
      "原文 original quotation", "原始假设，不应翻译", "Source explanation stays unchanged", "close / open"]) expect(html).toContain(phrase);
    expect(html).toContain('data-review-revision="frozen-v1"');
    expect(html).toContain('data-review-attempt="attempt"');
    expect(html).toContain('data-review-digest="digest"');
    expect(html).not.toContain("本次候选输出");
  });
  it("keeps one selected chart with four real metric buttons and exact cumulative backend values across gaps", () => {
    const html = english(createElement(CandidateSeriesView, { series }));
    expect(html.match(/<figure /g)).toHaveLength(1);
    expect(html.match(/aria-pressed=/g)).toHaveLength(4);
    expect(html).toContain('aria-pressed="true">Arithmetic cumulative gross return');
    expect(html.match(/data-segment-length="1"/g)).toHaveLength(2);
    expect(html).toContain('data-value="0"');
    expect(html).toContain('data-value="0.0379" data-date="2026-01-05"');
    expect(html).toContain("not compounded NAV or BRAIN PnL");
    expect(html).toContain("Forward returns are constant");
    expect(html).toContain("Original limitation text");
    expect(html).not.toContain("NaN");
  });
  it("does not draw invented return values for a wholly missing series", () => {
    const empty = { ...series, points: series.points.map(point => ({ ...point, gross_return: null, cumulative_gross_return: null })) };
    const html = english(createElement(CandidateSeriesView, { series: empty }));
    expect(html).not.toContain("<svg"); expect(html).toContain("There are no plottable values");
    expect(html).not.toContain('data-value="0"');
    expect(html).toContain("Candidate daily calculation details");
  });
  it("only offers review dimensions that survive durable draft recovery while requiring an explicit verdict", () => {
    const html = english(createElement(ResearchReviewForm, {
      workspaceId: "workspace", run, allowNewReview: true, candidates: run.state!.candidates!, paperId: "paper",
      artifacts: [], busy: "", onPerform: async (_label, action) => { await action(); }, refresh: async () => {},
    }));
    const select = html.match(/<label[^>]*>Review dimension<\/label><select[^>]*>([\s\S]*?)<\/select>/)?.[1];
    expect(select).toBeDefined();
    const categories = [...(select || "").matchAll(/<option value="([^"]*)"/g)].map(match => match[1]);
    expect(categories.length).toBeGreaterThan(0);
    expect(categories).not.toContain("");
    expect(select).toContain('<option value="implementation" selected="">');
    const values = new Map<string, string>();
    const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
    for (const category of categories) {
      const draft = { ...initialReviewDraft(), category, note: "Unsubmitted evidence judgment" };
      saveReviewDraft(storage, "frozen-review", draft);
      expect(loadReviewDraft(storage, "frozen-review")?.value).toEqual(draft);
    }
    expect(html).toContain('<option value="" selected="">Choose a verdict</option>');
    expect(() => requireReviewVerdict(initialReviewDraft().verdict)).toThrow();
  });
  it("places actual output before the form, keeps the verdict blank and preserves server-save recovery controls", () => {
    const html = english(createElement(ResearchReviewForm, {
      workspaceId: "workspace", run, allowNewReview: true, candidates: run.state!.candidates!, paperId: "paper",
      artifacts: [], busy: "", onPerform: async (_label, action) => { await action(); }, refresh: async () => {},
    }));
    expect(html.indexOf('aria-label="Current candidate output"')).toBeLessThan(html.indexOf('aria-label="Record this review"'));
    expect(html).toContain('<option value="" selected="">Choose a verdict</option>');
    expect(html).toContain("Review rationale");
    expect(html).toContain("Saving creates a server review record for this frozen result.");
    expect(html).toContain("Paper fidelity"); expect(html).toContain("Investment performance");
    expect(html).toContain('aria-labelledby="review-candidate-leave-title"');
    expect(html).toContain("Keep draft and switch");
    expect(html).not.toContain("simulation");
  });
});
