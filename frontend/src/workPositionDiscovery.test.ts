import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import DailyWorkflow from "./components/DailyWorkflow";
import type { Research, Run } from "./domain";

const research = { id: "r", revisions: [{ id: "v1", number: 1 }] } as Research;
const run = (id: string, status = "completed", research_id = "r", created_at = "2026-10-01"): Run =>
  ({ id, status, research_id, revision_id: "v1", mode: "normalized_fixed", created_at, attempt_count: 1 });
const render = (props: Partial<Parameters<typeof DailyWorkflow>[0]> = {}) =>
  renderToStaticMarkup(createElement(DailyWorkflow, { research, stage: "materials", run: null, editing: false, onStep: () => {}, ...props }));

describe("existing experiment discovery", () => {
  it("offers an explicit same-study result entry without selecting it on render", () => {
    const onOpenRun = vi.fn();
    const rows = [run("older"), run("newer", "completed", "r", "2026-10-02")];
    const html = render({ researchRuns: rows, onOpenRun });
    expect(html).toContain("已有实验可查看，尚未选择");
    expect(html).toContain("2 个已结束实验");
    expect(html).toContain("查看已有实验");
    expect(html).not.toContain("等待可核验结果");
    expect(onOpenRun).not.toHaveBeenCalled();
    expect(rows.map(r => r.id)).toEqual(["older", "newer"]);
  });
  it("does not advertise foreign or unfinished runs as completed results", () => {
    const html = render({ researchRuns: [run("foreign", "completed", "other"), run("active", "running")] });
    expect(html).toContain("等待可核验结果");
    expect(html).not.toContain("查看已有实验");
    expect(html).not.toContain("已结束实验");
  });
  it("keeps the selected historical run's status instead of the new result prompt", () => {
    const selected = { ...run("selected"), verification: { verified: true } };
    const html = render({ run: selected, stage: "review", researchRuns: [selected, run("latest")] });
    expect(html).toContain("可查看并判断");
    expect(html).toContain("selected");
    expect(html).not.toContain("尚未选择实验");
    expect(html).not.toContain("查看已有实验");
  });
  it("does not claim completed or failed catalog rows are already verified", () => {
    const html = render({ researchRuns: [run("failed", "failed")], onOpenRun: () => {} });
    expect(html).toContain("已结束实验");
    expect(html).not.toContain("可查看并判断");
  });
});
