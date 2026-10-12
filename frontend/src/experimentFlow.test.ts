import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import TaskEditor, { localizeTaskIssue, TaskConfigurationSummary } from "./components/TaskEditor";
import RunSubmission from "./components/RunSubmission";
import MutationRecovery from "./components/MutationRecovery";
import { LanguageProvider, LANGUAGE_KEY } from "./i18n";
import { emptyTask } from "./taskEditor";
import type { Dataset, Revision, Task } from "./domain";
import type { PendingMutation } from "./reviewRecovery";

const dataset: Dataset = { id: "dataset-fixed", title: "原始数据标题", version: "synthetic-v7", data_kind: "synthetic", sha256: "c".repeat(64), fields: ["open", "close"], metadata: {} };
function task(): Task {
  return { ...emptyTask(), candidates: [
    { id: "alpha006", hypothesis_id: "h", expression: "-correlation(open, volume, 10)", origin: "paper_original", changes: [] },
    { id: "alpha005", hypothesis_id: "h", expression: "rank(vwap)", origin: "paper_original", changes: [] },
  ], evaluation: { splits: { train: { start: "2022-01-03", end: "2022-09-30" }, validation: { start: "2022-10-03", end: "2023-03-31" }, test: { start: "2023-04-03", end: "2023-07-14" } }, min_assets: 5 } };
}
function render(element: ReactElement, locale: "zh" | "en") {
  vi.stubGlobal("localStorage", { getItem: (key: string) => key === LANGUAGE_KEY ? locale : null });
  return renderToStaticMarkup(createElement(LanguageProvider, null, element));
}
afterEach(() => vi.unstubAllGlobals());

describe("readable experiment configuration", () => {
  it("shows all three saved windows and the full candidate set without mutating the task", () => {
    const value = task(), before = JSON.stringify(value);
    const html = render(createElement(TaskConfigurationSummary, { task: value, dataset }), "en");
    for (const text of ["Training window", "Validation window", "Held-out test window", "2022-01-03", "2022-09-30", "2022-10-03", "2023-03-31", "2023-04-03", "2023-07-14", "All 2 candidates", "alpha006", "alpha005", "synthetic-v7", "原始数据标题", dataset.sha256]) expect(html).toContain(text);
    expect(html).toContain("this run computes no factors or metrics for this window");
    expect(html).toContain("The full data file is still loaded and checked");
    expect(html).toContain("Provider context excludes market rows, final-test bounds and results");
    expect(html).not.toContain("this run will not access it");
    expect(html).toContain("open[t+2] / open[t+1] - 1");
    expect(html).toContain("gross of costs");
    expect(JSON.stringify(value)).toBe(before);
  });
  it("labels unavailable date bounds instead of inserting sample dates", () => {
    const html = render(createElement(TaskConfigurationSummary, { task: emptyTask() }), "en");
    expect(html.match(/Not set/g)).toHaveLength(6);
    expect(html).not.toContain("2022-");
    expect(html).toContain("No candidates yet");
  });
  it("keeps advanced JSON and raw formulas while translating form controls", () => {
    const value = task(), raw = JSON.stringify(value, null, 2);
    const english = render(createElement(TaskEditor, { value: raw, onChange: () => {}, paper: null, section: "experiment", showIssues: false }), "en");
    const chinese = render(createElement(TaskEditor, { value: raw, onChange: () => {}, paper: null, section: "experiment", showIssues: false }), "zh");
    expect(english).toContain("train start date"); expect(chinese).toContain("train 开始日期");
    for (const html of [english, chinese]) {
      expect(html).toContain("-correlation(open, volume, 10)");
      expect(html).toContain("rank(vwap)");
      expect(html.match(/type="date"/g)).toHaveLength(6);
    }
    expect(english).toContain("Advanced: edit task JSON");
    expect(chinese).toContain("本次不计算此区间的因子或指标");
    expect(chinese).toContain("原数据文件仍会完整载入并检查");
    expect(chinese).toContain("Provider 上下文不包含行情行、最终测试区间边界或结果");
    expect(chinese).not.toContain("本次不会访问");
    expect(JSON.stringify(value, null, 2)).toBe(raw);
  });
  it("shows the saved revision, offline queue boundary and actual non-AI modes", () => {
    const revision: Revision = { id: "revision-7", research_id: "research", number: 7, task: task(), note: "", digest: "a".repeat(64), created_at: "2026-10-08T00:00:00Z" };
    const html = render(createElement(RunSubmission, { workspaceId: "workspace", researchId: "research", revision, dataset, workerOnline: false, busy: "", onQueued: async () => {} }), "en");
    expect(html).toContain("Execute v7");
    expect(html).toContain("Bounded rule-based repair");
    expect(html).toContain('value="agent"');
    expect(html).toContain("The worker is offline");
    expect(html).toContain("complete candidate set saved in v7");
    expect(html).toContain("cannot establish investment performance");
  });
});

describe("localized diagnostics and recovery boundaries", () => {
  it("translates known form warnings while preserving raw identifiers and opaque errors", () => {
    const en = (_zh: string, english: string) => english;
    expect(localizeTaskIssue("所需字段缺失：vwap, custom_field。依赖这些字段的候选将被阻断；系统不会替换字段。", en)).toContain("vwap, custom_field");
    expect(localizeTaskIssue("validation 至少包含 3 个交易日，且各区间必须依次排列、不重叠。", en)).toContain("non-overlapping");
    expect(localizeTaskIssue("HTTP 409 · request_conflict · 原始服务错误", en)).toBe("HTTP 409 · request_conflict · 原始服务错误");
  });
  it("an uncertain request exposes only safe retry and retains its frozen identity", () => {
    const pending: PendingMutation = { version: 1, scope: "original-workspace", path: "/researches/original/revisions", savedAt: "2026-10-08T00:00:00Z", state: "unconfirmed", body: { base_revision_id: "original-v1", note: "保留用户原文", idempotency_key: "original-key" } };
    const before = JSON.stringify(pending);
    const html = render(createElement(MutationRecovery, { name: "修订", pending, error: "", busy: false, onRetry: () => {}, onDismiss: () => {} }), "en");
    expect(html).toContain("Safely retry revision submission");
    expect(html).toContain("original-v1"); expect(html).toContain("original-key"); expect(html).toContain("保留用户原文");
    expect(html).not.toContain("Release rejected");
    expect(JSON.stringify(pending)).toBe(before);
    const rejected = render(createElement(MutationRecovery, { name: "修订", pending: { ...pending, state: "rejected" }, error: "", busy: false, onRetry: () => {}, onDismiss: () => {} }), "en");
    expect(rejected).toContain("Release rejected revision request");
    expect(rejected).not.toContain("Safely retry revision submission");
  });
});
