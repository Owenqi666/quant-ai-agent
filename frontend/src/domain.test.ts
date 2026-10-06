import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import QualityPanel from "./components/QualityPanel";
import {
  canReview,
  compatibleCases,
  formatNumber,
  parseTask,
  reviewPayload,
  regressionOutcome,
} from "./domain";
import type { CandidateState, RegressionCase, RegressionCheck, Research, Run } from "./domain";

const task = {
  evidence: [],
  hypotheses: [],
  candidates: [],
  evaluation: {},
  budget: {},
};
describe("research input boundaries", () => {
  it("rejects filesystem injection rather than silently selecting local resources", () => {
    expect(() =>
      parseTask(JSON.stringify({ ...task, paper_pdf: "/etc/passwd" })),
    ).toThrow("路径");
  });
  it("rejects malformed JSON, arrays, and invalid contract fields before submission", () => {
    expect(() => parseTask("{")).toThrow("JSON");
    expect(() => parseTask("[]")).toThrow("对象");
    expect(() =>
      parseTask(JSON.stringify({ ...task, candidates: {} })),
    ).toThrow("candidates");
    expect(() => parseTask(JSON.stringify({ ...task, budget: [] }))).toThrow(
      "budget",
    );
  });
  it("preserves scientific fields while removing only optional presentation metadata", () => {
    expect(
      parseTask(
        JSON.stringify({ ...task, title: "source", schema_version: 1 }),
      ),
    ).toEqual(task);
  });
});

describe("saved regression interpretation", () => {
  const research: Research = {
    id: "r", title: "Fixture", paper_id: "p", dataset_id: "d",
    latest_revision_id: "revision", created_at: "2026-09-30T00:00:00Z",
  };
  const run: Run = {
    id: "run", research_id: "r", revision_id: "revision", status: "completed",
    mode: "fixed", attempt_count: 1, created_at: "2026-09-30T00:00:00Z",
  };
  function panel(check: RegressionCheck) {
    return renderToStaticMarkup(createElement(QualityPanel, {
      workspaceId: "test-workspace", research, run, runs: [run], selectedRun: run.id, artifacts: [], cases: [],
      checks: [check], busy: "", onSelectRun: () => {},
      onPerform: async (_label: string, action: () => Promise<void>) => { await action(); },
      refresh: async () => {},
    }));
  }
  const base: RegressionCheck = {
    id: "check", run_id: "run", created_at: "2026-09-30T00:00:00Z",
    scope: "frozen contract and independent reference", passed: false,
    outcome: "not_comparable", results: [{
      case_id: "case", candidate_id: "alpha006", expected_status: "evaluated",
      actual_status: null, passed: false, compatible: false, reason: "Formula changed",
      outcome: "not_comparable", differences: ["formula"], checks: [],
    }],
  };
  it("renders incomparable research separately without a red failure or green success badge", () => {
    const html = panel(base);
    expect(html).toContain("存在不可比较项");
    expect(html).toContain("条件差异：formula");
    expect(html).not.toContain("status-failed");
    expect(html).not.toContain("status-evaluated");
    expect(html).not.toContain("全部通过");
  });
  it("keeps individual reference failures inspectable even when candidate status matches", () => {
    const html = panel({
      ...base, outcome: "failed", results: [{
        ...base.results[0], compatible: true, actual_status: "evaluated", outcome: "failed",
        checks: [{ name: "numeric_reference", outcome: "failed", reason: "Independent reference mismatch", mismatches: ["daily[0].assets[0].weight"], oracle_version: "fixture-v1", absolute_tolerance: 1e-10, expected: 0.5, actual: 0.8 }],
      }],
    });
    expect(html).toContain("存在不符合预期");
    expect(html).toContain("numeric_reference");
    expect(html).toContain("Independent reference mismatch");
    expect(html).toContain("daily[0].assets[0].weight");
    expect(html).toContain("fixture-v1");
    expect(html).toContain("0.5");
    expect(html).toContain("0.8");
  });
  it("identifies a historical status-only result instead of implying stronger verification", () => {
    const html = panel({ ...base, outcome: undefined, passed: true, results: [], scope: "status regression" });
    expect(html).toContain("历史记录仅检查状态");
    expect(html).toContain("status regression");
  });
});
describe("result trust boundaries", () => {
  it("distinguishes undefined metrics from a legitimate zero", () => {
    expect(formatNumber(null)).toBe("—");
    expect(formatNumber(undefined)).toBe("—");
    expect(formatNumber(NaN)).toBe("—");
    expect(formatNumber(Infinity)).toBe("—");
    expect(formatNumber(0)).toBe("0.0000");
  });
  it("only permits completed or failed attempts with an explicit verified result", () => {
    const run: Run = {
      id: "run",
      revision_id: "revision",
      research_id: "research",
      mode: "fixed",
      created_at: "2026-09-30T00:00:00Z",
      attempt_count: 1,
      status: "completed",
      verification: { verified: true },
    };
    expect(canReview(run)).toBe(true);
    expect(canReview({ ...run, status: "failed" })).toBe(true);
    for (const status of ["running", "queued", "cancelled", "interrupted"])
      expect(canReview({ ...run, status })).toBe(false);
    expect(canReview({ ...run, verification: { verified: false } })).toBe(
      false,
    );
    expect(canReview({ ...run, verification: { valid: true } })).toBe(false);
  });
  it("requires actual written review evidence", () => {
    expect(() =>
      reviewPayload("alpha006", "accepted", "evidence", "  "),
    ).toThrow("审核依据");
    expect(() => reviewPayload("", "accepted", "evidence", "verified")).toThrow(
      "候选",
    );
    expect(
      reviewPayload("alpha006", "accepted", "evidence", " checked quote "),
    ).toEqual({
      candidate_id: "alpha006",
      verdict: "accepted",
      category: "evidence",
      note: "checked quote",
      source: "human",
    });
  });
  it("excludes regression expectations from other research, paper, dataset or candidate", () => {
    const research = { id: "r", paper_id: "p", dataset_id: "d" } as Research;
    const c = {
      id: "case",
      research_id: "r",
      paper_id: "p",
      dataset_id: "d",
      candidate_id: "alpha006",
      contract: { formula: "ts_corr(open, volume, 10)" },
      contract_digest: "frozen",
      review_id: "review", expected_status: "evaluated", note: "Approved fixture",
      created_at: "2026-09-30T00:00:00Z", version: 2,
    } as RegressionCase;
    const cases = [
      c,
      { ...c, id: "research", research_id: "other" },
      { ...c, id: "paper", paper_id: "other" },
      { ...c, id: "data", dataset_id: "other" },
      { ...c, id: "candidate", candidate_id: "alpha005" },
      { ...c, id: "legacy", contract: null, contract_digest: null },
    ];
    expect(
      compatibleCases(cases, research, [{ id: "alpha006" } as CandidateState]),
    ).toEqual([c]);
    expect(compatibleCases(cases, null, [])).toEqual([]);
  });
  it("only screens identity; leaves formula and other frozen conditions to the backend", () => {
    const research = { id: "r", paper_id: "p", dataset_id: "d" } as Research;
    const c = {
      id: "case", research_id: "r", paper_id: "p", dataset_id: "d",
      candidate_id: "alpha006", contract: { formula: "ts_corr(open, volume, 10)" },
      contract_digest: "frozen",
      review_id: "review", expected_status: "evaluated", note: "Approved fixture",
      created_at: "2026-09-30T00:00:00Z", version: 2,
    } as RegressionCase;
    expect(compatibleCases([c], research, [{
      id: "alpha006", expression: "ts_corr(open, volume, 5)",
    } as CandidateState])).toEqual([c]);
  });
  it("shows not-comparable separately from failed and never lets a legacy boolean override it", () => {
    expect(regressionOutcome({ passed: true, outcome: "not_comparable" })).toBe("not_comparable");
    expect(regressionOutcome({ passed: false, outcome: "failed" })).toBe("failed");
    expect(regressionOutcome({ passed: true })).toBe("passed");
    expect(regressionOutcome({ passed: false })).toBe("failed");
  });
});
