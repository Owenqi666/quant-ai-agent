import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import ResearchStageContent from "./components/ResearchStageContent";
import DailyWorkflow from "./components/DailyWorkflow";
import { LanguageProvider } from "./i18n";
import type { Dataset, Paper, Research, Revision, Task } from "./domain";

const task: Task = {
  evidence: [{ id: "source-1", page: 8, quote: "Original source: correlation(open, volume, 10)" }],
  hypotheses: [{ id: "hypothesis-1", claim: "用户保留的主张", attribution: "paper_original", evidence_ids: ["source-1"], economic_mechanism: "A conjecture, not a source conclusion.", mechanism_attribution: "model_conjecture", signal_direction: "Original signal direction", required_fields: ["open", "volume"], assumptions: ["Original author-supplied constraint"] }, { id: "hypothesis-2", claim: "VWAP dependency", attribution: "user_modification", evidence_ids: [], economic_mechanism: "User explanation", mechanism_attribution: "user_modification", signal_direction: "Original direction", required_fields: ["vwap"], assumptions: [] }],
  candidates: [{ id: "candidate-alias", hypothesis_id: "hypothesis-1", expression: "correlation(open, volume, 10)", origin: "paper_original", changes: [] }, { id: "candidate-missing", hypothesis_id: "hypothesis-2", expression: "vwap", origin: "user_modification", changes: ["User change note"] }],
  evaluation: { min_assets: 5, splits: { train: { start: "2022-01-01", end: "2022-06-30" }, validation: { start: "2022-07-01", end: "2022-12-31" }, test: { start: "2023-01-01", end: "2023-06-30" } } },
  budget: { max_attempts_per_candidate: 2, max_candidates: 2, max_seconds: 60, max_tool_calls: 10 },
};
const dataset: Dataset = { id: "dataset-1", title: "Original dataset title", version: "version-1", sha256: "dataset-digest", data_kind: "synthetic", fields: ["open", "volume"], metadata: { source: "Original provenance" } };
const paper: Paper = { id: "paper-1", title: "Original paper title", sha256: "paper-digest" };
const revision: Revision = { id: "revision-1", research_id: "research-1", number: 1, task, note: "", digest: "revision-digest", created_at: "2026-10-08T00:00:00Z" };
const research: Research = { id: "research-1", title: "Original research title", paper_id: paper.id, dataset_id: dataset.id, latest_revision_id: revision.id, created_at: revision.created_at, revisions: [revision], preflight: { dataset_version: dataset.version, fields: dataset.fields, scope: "Static suitability only", candidates: [{ candidate_id: "candidate-alias", status: "warning", code: "alias_requires_normalization", expression_changed: false, suggested_expression: "ts_corr(open, volume, 10)", warmup_rows: 9 }, { candidate_id: "candidate-missing", status: "blocked", code: "required_field_missing", expression_changed: false, missing_fields: ["vwap"] }] } };
type Props = Parameters<typeof ResearchStageContent>[0];
const props: Props = { stage: "evidence", research, revision, paper, dataset, onStep: () => {}, onEdit: () => {} };
const render = (overrides: Partial<Props> = {}) => renderToStaticMarkup(createElement(ResearchStageContent, { ...props, ...overrides }));
afterEach(() => vi.unstubAllGlobals());

describe("live research reading boundaries", () => {
  it("does not claim a citation match until the corresponding page text is loaded", () => {
    expect(render()).toContain("引用匹配待核对");
    expect(render()).not.toContain("已在本页提取文本中匹配");
    expect(render({ paper: { ...paper, pages: [{ page: 8, text: task.evidence[0].quote }] } })).toContain("已在本页提取文本中匹配");
    const mismatch = render({ paper: { ...paper, pages: [{ page: 8, text: "Different extracted content" }] } });
    expect(mismatch).toContain("未在本页提取文本中匹配，请核对 PDF");
    expect(mismatch).toContain(task.evidence[0].quote);
  });
  it("renders missing evidence and linked hypotheses explicitly", () => {
    const withMissingEvidence = { ...revision, task: { ...task, evidence: [] } };
    expect(render({ revision: withMissingEvidence })).toContain("缺少证据记录：source-1");
    const noHypothesis = { ...revision, task: { ...task, hypotheses: [] } };
    expect(render({ stage: "candidates", revision: noHypothesis })).toContain("缺少关联假说：hypothesis-1");
  });
  it("never applies latest-version or mismatched dataset preflight to the selected version", () => {
    const historical = render({ stage: "candidates", revision: { ...revision, id: "historical-revision" } });
    expect(historical).toContain("不使用其他版本的预检结论");
    expect(historical).not.toContain("alias_requires_normalization");
    expect(historical).not.toContain("ts_corr(open, volume, 10)");
    const wrongDataVersion = render({ stage: "candidates", dataset: { ...dataset, version: "version-2" } });
    expect(wrongDataVersion).toContain("不使用其他版本的预检结论");
    expect(wrongDataVersion).not.toContain("alias_requires_normalization");
  });
  it("labels normalized expressions as suggestions while preserving declared formulas", () => {
    const html = render({ stage: "candidates" });
    expect(html).toContain("correlation(open, volume, 10)");
    expect(html).toContain("ts_corr(open, volume, 10)");
    expect(html).toContain("预检建议表达式");
    expect(html).toContain("不是实际执行记录");
    expect(html).toContain("alias_requires_normalization");
  });
  it("preserves blocked candidates and does not turn a reading selection into an execution subset", () => {
    const html = render({ stage: "candidates", revision: { ...revision, task: { ...task, candidates: [...task.candidates].reverse() } } });
    expect(html).toContain("全部 2 个候选");
    expect(html).toContain("candidate-alias");
    expect(html).toContain("candidate-missing");
    expect(html).toContain("vwap · 缺失");
    expect(html).toContain("required_field_missing");
    expect(html).toContain("不得隐式替代字段");
    expect(html).toContain("完整候选集合");
    expect(html).not.toContain("disabled=\"\"");
  });
  it("keeps the empty state actionable but prevents experiment navigation with no candidates", () => {
    const html = render({ stage: "candidates", revision: { ...revision, task: { ...task, candidates: [] } } });
    expect(html).toContain("尚未定义候选");
    expect(html).toContain("创建新版本");
    expect(html).toMatch(/disabled="">继续配置实验/);
  });
  it("rejects stale source objects from another research binding", () => {
    const html = render({ stage: "materials", paper: { ...paper, id: "other-paper", title: "Foreign paper" }, dataset: { ...dataset, id: "other-dataset", title: "Foreign dataset" } });
    expect(html).not.toContain("Foreign paper");
    expect(html).not.toContain("Foreign dataset");
    expect(html).toContain("尚未载入数据字段");
    expect(html).not.toContain("本版本有未满足的数据条件");
  });
  it("translates controls while keeping paper and user content in its original language", () => {
    vi.stubGlobal("localStorage", { getItem: () => "en" });
    const html = renderToStaticMarkup(createElement(LanguageProvider, { children: createElement(ResearchStageContent, props) }));
    expect(html).toContain("From source text to research hypothesis");
    expect(html).toContain("Create new version");
    expect(html).toContain("用户保留的主张");
    expect(html).toContain(task.evidence[0].quote);
    expect(html).toContain(task.hypotheses[0].assumptions[0]);
    expect(html).toContain("citation matching is unverified");
  });
  it("keeps English workflow identity distinct from an older frozen experiment", () => {
    vi.stubGlobal("localStorage", { getItem: () => "en" });
    const html = renderToStaticMarkup(createElement(LanguageProvider, { children: createElement(DailyWorkflow, { research, revision: { ...revision, id: "revision-2", number: 2 }, stage: "review", editing: true, onStep: () => {}, run: { id: "run-1", research_id: research.id, revision_id: revision.id, status: "running", mode: "fixed", created_at: revision.created_at, attempt_count: 1 } }) }));
    expect(html).toContain("Version for submission: v2");
    expect(html).toContain("selected experiment belongs to v1");
    expect(html).toContain("Running");
    expect(html).toContain("Version edits are unsaved");
    expect(html).toContain("changing stages does not submit another experiment");
    expect(html.match(/aria-current="step"/g)).toHaveLength(1);
  });
});
