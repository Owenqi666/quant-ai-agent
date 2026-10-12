import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { editableTask } from "./domain";
import type { Task } from "./domain";
import { draftKey, isResearchDraft, readDraft } from "./drafts";
import { editEconomicMechanism, FORMULA_ENGINEERING_ASSUMPTION, formTask, nextId, quoteMatches, taskDifferences, taskFormIssues } from "./taskEditor";

const task = () => editableTask(JSON.parse(readFileSync(new URL("../../examples/alpha101/task.json", import.meta.url), "utf8")) as Task);

describe("manual scientific authoring", () => {
  it("preserves attribution, direction and exact expressions in the shared form model", () => {
    const original = task();
    expect(formTask(JSON.stringify(original))).toEqual(original);
    expect(taskFormIssues(original)).toEqual([]);
    expect(original.hypotheses[0].mechanism_attribution).toBe("model_conjecture");
  });
  it("does not crash a form on malformed nested advanced input", () => {
    for (const invalid of [null, {}, { ...task().hypotheses[0], assumptions: "line" }, { ...task().hypotheses[0], attribution: undefined }]) {
      expect(() => formTask(JSON.stringify({ ...task(), hypotheses: [invalid] }))).toThrow();
    }
  });
  it("checks literal source text only, preserving scientific attribution and reference errors", () => {
    expect(quoteMatches("Ａ\nB", "start A   B end")).toBe(true);
    expect(quoteMatches("invented", "original")).toBe(false);
    expect(quoteMatches("", "original")).toBe(false);
    const changed = task();
    changed.hypotheses[0].evidence_ids = ["deleted"];
    changed.candidates[0].changes = ["window changed"];
    const errors = taskFormIssues(changed, { id: "paper", title: "paper", sha256: "digest", pages: [{ page: 8, text: "does not match" }] });
    expect(errors.some((e) => e.includes("引用未"))).toBe(true);
    expect(errors.some((e) => e.includes("现存证据"))).toBe(true);
    expect(errors.some((e) => e.includes("论文原式不应"))).toBe(true);
  });
  it("generates unused IDs and reports scientific changes without rewriting their meaning", () => {
    expect(nextId("evidence", [{ id: "evidence-1" }, { id: "evidence-3" }])).toBe("evidence-2");
    const before = task(), after = task();
    after.candidates[0].expression = "ts_corr(open, volume, 5)";
    after.candidates[0].origin = "user_modification";
    after.candidates[0].changes = ["window 10 to 5"];
    const diffs = taskDifferences(before, after);
    expect(diffs.map((d) => d.path).sort()).toEqual(["任务.candidates[alpha006].changes", "任务.candidates[alpha006].expression", "任务.candidates[alpha006].origin"]);
    expect(before.candidates[0].expression).toContain("10");
  });
  it("reports candidate order changes because bounded budgets can change which candidate runs", () => {
    const before = task(), after = task();
    after.candidates.reverse();
    expect(taskDifferences(before, after)).toEqual([{ path: "任务.candidates.顺序", before: ["alpha006", "alpha101", "alpha005"], after: ["alpha005", "alpha101", "alpha006"] }]);
  });
  it("allows evidence editing before candidates and budgets are complete without weakening final submission", () => {
    const draft = task();
    draft.candidates = [];
    draft.budget.max_tool_calls = 0;
    expect(taskFormIssues(draft, null, "evidence")).toEqual([]);
    expect(taskFormIssues(draft, null, "candidates")).toEqual(["候选需要 1–100 条。"]);
    expect(taskFormIssues(draft, null, "experiment")).toEqual(["max_tool_calls 需要为 1–200 的整数。"]);
    expect(taskFormIssues(draft)).toEqual(["候选需要 1–100 条。", "max_tool_calls 需要为 1–200 的整数。"]);
  });
  it("still catches broken candidate links and source attribution in its own editing stage", () => {
    const draft = task();
    draft.evidence[0].quote = "";
    draft.candidates[0].hypothesis_id = "removed";
    draft.candidates[0].origin = "user_modification";
    expect(taskFormIssues(draft, null, "candidates")).toEqual([
      "候选 1：请填写表达式并关联假设。", "候选 1：论文原式不应记录改动，修改或推测必须描述改动。",
    ]);
    expect(taskFormIssues(draft, null, "evidence")).toContain("证据 1：请选择页码并填写原文。");
    expect(taskFormIssues(draft)).toHaveLength(3);
  });
  it("checks all source and identity invariants at save after navigation or advanced JSON edits", () => {
    const draft = task();
    draft.evidence.push({ ...draft.evidence[0] });
    draft.hypotheses[0].evidence_ids = ["removed"];
    draft.candidates[0].id = "invalid id";
    draft.budget.max_seconds = 1000;
    const all = taskFormIssues(draft);
    const staged = (["evidence", "candidates", "experiment"] as const).flatMap((section) => taskFormIssues(draft, null, section));
    expect([...all].sort()).toEqual([...staged].sort());
    expect(all.some((message) => message.includes("重复标识"))).toBe(true);
    expect(all.some((message) => message.includes("现存证据"))).toBe(true);
    expect(all.some((message) => message.includes("候选标识"))).toBe(true);
    expect(all.some((message) => message.includes("max_seconds"))).toBe(true);
  });
});

describe("explicit null mechanism editing", () => {
  const source = () => {
    const value = task();
    value.hypotheses[0] = { ...value.hypotheses[0], economic_mechanism: null, mechanism_attribution: "unresolved",
      assumptions: [...value.hypotheses[0].assumptions, FORMULA_ENGINEERING_ASSUMPTION,
        "Project configuration: unresolved dispositions v1=" + JSON.stringify([{ path: "/unresolved_rules/0", original: "[scientific] " + "source text ".repeat(4000), disposition: "scientific_unresolved", rationale: "Retain science as unknown." }])] };
    return value;
  };
  it("round-trips the exact null/unresolved branch and all long engineering conditions without an explanation", () => {
    const original = source(), parsed = formTask(JSON.stringify(original));
    expect(parsed).toEqual(original); expect(taskFormIssues(parsed)).toEqual([]);
    const unchanged = editEconomicMechanism(parsed.hypotheses[0], "");
    expect(unchanged).toEqual(original.hypotheses[0]);
    expect(JSON.stringify({ ...parsed, hypotheses: [unchanged, ...parsed.hypotheses.slice(1)] })).toContain('"economic_mechanism":null');
  });
  it("requires exact engineering scope for null and keeps ordinary scientific validation strict", () => {
    const invalid = source();
    invalid.hypotheses[0].assumptions = [];
    expect(() => formTask(JSON.stringify(invalid))).toThrow(/空机制仅限/);
    const wrong = source();
    wrong.hypotheses[0] = { ...wrong.hypotheses[0], mechanism_attribution: "paper_original" } as Task["hypotheses"][number];
    expect(() => formTask(JSON.stringify(wrong))).toThrow(/空机制仅限/);
    const blank = task(); blank.hypotheses[0].economic_mechanism = "";
    expect(taskFormIssues(blank)).toContain("假设 1：请填写主张、经济机制和信号方向。");
  });
  it("labels a newly supplied mechanism as a user modification in the ordinary branch", () => {
    const original = source(), before = original.hypotheses[0];
    const edited = editEconomicMechanism(before, "My explicit interpretation, not a proven paper mechanism.");
    expect(edited.economic_mechanism).toBe("My explicit interpretation, not a proven paper mechanism.");
    expect(edited.mechanism_attribution).toBe("user_modification");
    expect(edited.assumptions).toEqual(before.assumptions.filter(item => item !== FORMULA_ENGINEERING_ASSUMPTION));
    expect(edited.assumptions.at(-1)).toBe(before.assumptions.at(-1));
    expect(before.economic_mechanism).toBeNull();
    expect(taskFormIssues({ ...original, hypotheses: [edited, ...original.hypotheses.slice(1)] })).toEqual([]);
  });
});

describe("explicit draft recovery boundaries", () => {
  const value = { title: "Research draft", paperId: "p", datasetId: "d", taskText: "{}", note: "revision reason" };
  it("isolates workspaces, research identities and base revisions", () => {
    const key = draftKey("workspace-a", "revision:r:v1");
    const raw = JSON.stringify({ version: 1, scope: key, savedAt: "2026-10-01T00:00:00Z", value });
    const storage = { getItem: (candidate: string) => candidate === key ? raw : null };
    expect(readDraft(storage, key, isResearchDraft)?.value).toEqual(value);
    for (const other of [draftKey("workspace-b", "revision:r:v1"), draftKey("workspace-a", "revision:r:v2"), draftKey("workspace-a", "revision:other:v1")]) expect(readDraft(storage, other, isResearchDraft)).toBeNull();
  });
  it("rejects wrong-scope and incomplete drafts instead of applying them", () => {
    const key = draftKey("workspace", "create:blank");
    expect(() => readDraft({ getItem: () => JSON.stringify({ version: 1, scope: "different", savedAt: "now", value }) }, key, isResearchDraft)).toThrow();
    expect(() => readDraft({ getItem: () => JSON.stringify({ version: 1, scope: key, savedAt: "now", value: { title: "partial" } }) }, key, isResearchDraft)).toThrow();
  });
});
