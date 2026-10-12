import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import QualityPanel from "./components/QualityPanel";
import type { Research, Run } from "./domain";

const research: Research = {
  id: "research", title: "Review workflow fixture", paper_id: "paper", dataset_id: "data",
  latest_revision_id: "latest", created_at: "2026-10-08T00:00:00Z",
};
const run: Run = {
  id: "run", research_id: "research", revision_id: "historical", mode: "fixed", status: "completed",
  attempt_count: 1, created_at: "2026-10-08T00:00:00Z", verification: {verified: true},
  review_targets: [{candidate_id: "candidate", attempt_id: "attempt", result_digest: "digest"}],
  state: {
    task: {candidates: [{id: "candidate", hypothesis_id: "hypothesis", expression: "declared_close", origin: "user_modification", changes: []}]},
    candidates: [{id: "candidate", hypothesis_id: "hypothesis", expression: "close", origin: "user_modification", changes: [], status: "evaluated",
      result: {expression: "executed_close", metrics: {mean_rank_ic: 0}}}],
  },
};
function render(value: Run) {
  return renderToStaticMarkup(createElement(QualityPanel, {
    workspaceId: "workflow-test", research, run: value, runs: [value], selectedRun: value.id,
    artifacts: [], cases: [], checks: [], busy: "", onSelectRun: () => {}, onReport: () => {},
    onPerform: async (_label: string, action: () => Promise<void>) => action(), refresh: async () => {},
  }));
}

describe("production review workflow", () => {
  it("shows the selected frozen output before an explicitly empty judgment and retains historical identity", () => {
    const html = render(run);
    expect(html).toContain("你正在审核历史版本");
    expect(html).toContain('data-review-revision="historical"');
    expect(html).toContain("declared_close");
    expect(html).toContain("executed_close");
    expect(html.indexOf("候选计算指标")).toBeLessThan(html.indexOf("记录判断与理由"));
    expect(html).toContain('<option value="" selected="">请选择结论</option>');
    expect(html).not.toContain('<option value="accepted" selected="">');
    expect(html).not.toContain("继续为当前实验生成报告");
  });
  it("keeps regression and recording tools available but closed outside the main judgment", () => {
    const html = render(run);
    expect(html).toContain('<details class="review-development-tools"><summary>可选：回归验证与开发检查</summary>');
    expect(html).toContain('<details class="review-recording-options"><summary>可选：分项审核与记录设置</summary>');
    expect(html).toContain("明确批准为回归用例");
    expect(html).toContain("执行回归检查");
    expect(html).toContain("审核声明来源");
  });
  it("does not expose stale metrics or new judgment when integrity verification fails", () => {
    const html = render({...run, verification: {verified: false}});
    expect(html).toContain("当前输出尚未通过完整性校验");
    expect(html).not.toContain("候选计算指标");
    expect(html).not.toContain('aria-label="记录本次审核判断"');
  });
});
