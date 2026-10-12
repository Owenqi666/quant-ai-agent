import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Research, Revision, Run, Task } from "./domain";
import { FORMULA_ENGINEERING_ASSUMPTION, emptyTask } from "./taskEditor";
import ResearchStageContent from "./components/ResearchStageContent";
import TaskEditor from "./components/TaskEditor";
import ReviewEvidence from "./components/ReviewEvidence";
vi.mock("./i18n", () => ({ useI18n: () => ({ t: (zh: string) => zh }), readLocale: () => "zh", translate: (zh: string) => zh }));

const task: Task = { ...emptyTask(), evidence: [{ id: "e", page: 8, quote: "Source formula" }],
  hypotheses: [{ id: "h", claim: "Source formula only", attribution: "paper_original", evidence_ids: ["e"],
    economic_mechanism: null, mechanism_attribution: "unresolved", signal_direction: "Mathematical sign only",
    required_fields: ["close"], assumptions: [FORMULA_ENGINEERING_ASSUMPTION] }],
  candidates: [{ id: "c", hypothesis_id: "h", expression: "close", origin: "paper_original", changes: [] }] };

describe("unresolved mechanism stays explicit in editable and frozen views", () => {
  it("renders an empty mechanism editor with unresolved attribution and no replacement explanation", () => {
    const markup = renderToStaticMarkup(createElement(TaskEditor, { value: JSON.stringify(task), onChange: () => {}, paper: null, section: "evidence" }));
    expect(markup).toContain("未提供 / 未决；仅公式工程验证");
    expect(markup).toContain("保存继续保留 null，不补写解释");
    expect(task.hypotheses[0].economic_mechanism).toBeNull();
  });
  it("labels the research-stage mechanism as unknown without rewriting saved content", () => {
    const markup = renderToStaticMarkup(createElement(ResearchStageContent, { stage: "evidence",
      research: { id: "r", paper_id: "p", dataset_id: "d", latest_revision_id: "v" } as Research,
      revision: { id: "v", number: 1, task } as Revision, onStep: () => {}, onEdit: () => {} }));
    expect(markup).toContain("未提供 / 未决；仅公式工程验证，研究有效性仍待评定。");
    expect(markup).toContain("未决");
    expect(task.hypotheses[0].economic_mechanism).toBeNull();
  });
  it("keeps the null mechanism visible in verified frozen output review", () => {
    const run: Run = { id: "run", research_id: "r", revision_id: "v", status: "completed", mode: "normalized_fixed", created_at: "", attempt_count: 1,
      verification: { verified: true }, review_targets: [{ candidate_id: "c", attempt_id: "a", result_digest: "c".repeat(64) }],
      regression_target: { attempt_id: "a", result_digest: "s".repeat(64) },
      state: { task, candidates: [{ ...task.candidates[0], status: "evaluated", result: { expression: "close", metrics: {} } }] } };
    const markup = renderToStaticMarkup(createElement(ReviewEvidence, { run, candidateId: "c", artifacts: [] }));
    expect(markup).toContain("经济解释（未决）");
    expect(markup).toContain("未提供 / 未决；仅公式工程验证，研究有效性仍待评定。");
    expect(markup).not.toContain("未识别声明");
  });
});
