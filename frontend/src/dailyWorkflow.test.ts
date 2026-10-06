import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { currentRunReport, retainSelected, selectedResearchRun, revisionLabel } from "./dailyWorkflow";
import type { Research, Run } from "./domain";
import DailyWorkflow from "./components/DailyWorkflow";

const run: Run = { id: "historical-run", research_id: "research-a", revision_id: "revision-v1", status: "completed", mode: "normalized_fixed", created_at: "2026-10-02T00:00:00Z", attempt_count: 1 };
const research = { id: "research-a", revisions: [{ id: "revision-v1", number: 1 }, { id: "revision-v2", number: 2 }] } as Research;

describe("daily workflow identity and explicit report scope", () => {
  it("retains the explicit historical selection after a bounded first page refresh without duplication", () => {
    const rows = Array.from({length:50}, (_, index) => ({ id: `recent-${index}` }));
    const selection = { id: "older-selected" };
    const retained = retainSelected(rows, selection);
    expect(retained).toHaveLength(51);
    expect(retained.at(-1)).toBe(selection);
    expect(retainSelected(retained, selection)).toBe(retained);
    expect(retainSelected(rows, null)).toBe(rows);
  });
  it("never treats stale or cross-research run details as the current selection", () => {
    expect(selectedResearchRun("research-a", run.id, run)).toBe(run);
    expect(selectedResearchRun("research-b", run.id, run)).toBeNull();
    expect(selectedResearchRun("research-a", "new-run", run)).toBeNull();
  });
  it("builds exactly one selected run and refuses missing, foreign or still-running records", () => {
    expect(currentRunReport(research.id, run)).toEqual({ run_ids: [run.id] });
    expect(() => currentRunReport(research.id, null)).toThrow("请先选择");
    expect(() => currentRunReport("research-b", run)).toThrow("请先选择");
    expect(() => currentRunReport(research.id, {...run, status:"running"})).toThrow("结束");
    expect(currentRunReport(research.id, {...run, status:"failed"})).toEqual({run_ids:[run.id]});
  });
  it("keeps older result revision distinct from the next submitted revision and does not claim semantic approval", () => {
    const html = renderToStaticMarkup(createElement(DailyWorkflow, {research, revision:research.revisions![1], run, onStep:()=>undefined}));
    expect(html).toContain("待提交修订：v2");
    expect(html).toContain("当前实验绑定 v1");
    expect(html).toContain(run.id.slice(0, 8));
    expect(html).toContain("流程观测与主动计时均为可选");
    expect(revisionLabel(research, "missing-revision")).toContain("missing-");
    expect(html).not.toContain("研究已通过");
  });
});
