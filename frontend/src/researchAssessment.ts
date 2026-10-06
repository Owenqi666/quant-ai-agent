import type { ResearchAssessment } from "./generated/api-contract";

export const assessmentDimensions = {
  evidence_accuracy: "引用与页码准确",
  hypothesis_fidelity: "假设忠于所引原文",
  mechanism_attribution: "经济解释归因恰当",
  field_semantics: "字段含义与替代说明",
  implementation_alignment: "实现与声明公式一致",
} as const;
export type Dimension = keyof typeof assessmentDimensions;
export type AssessmentDraft = Omit<ResearchAssessment, "expected_attempt_id" | "expected_result_digest">;
export function emptyAssessment(): AssessmentDraft {
  return { reviewer: "", dimensions: Object.fromEntries(Object.keys(assessmentDimensions).map((key) =>
    [key, { outcome: "not_assessed", reason: "" }])) as AssessmentDraft["dimensions"], active_intervals: [] };
}
export function assessmentPayload(draft: AssessmentDraft, target: { attempt_id: string; result_digest: string } | undefined, running: string | null): ResearchAssessment {
  if (!target) throw new Error("当前输出尚未获得可审核的版本标识，请刷新后重试。");
  if (running) throw new Error("请先暂停主动工作计时，再保存审核。");
  if (!draft.reviewer.trim()) throw new Error("请填写审核人标识。");
  for (const [key, label] of Object.entries(assessmentDimensions)) {
    if (!draft.dimensions[key as Dimension].reason.trim()) throw new Error(`请填写“${label}”的判断理由，未评定也应说明原因。`);
  }
  return { ...draft, reviewer: draft.reviewer.trim(), expected_attempt_id: target.attempt_id, expected_result_digest: target.result_digest };
}
export function finishInterval(draft: AssessmentDraft, startedAt: string, endedAt: string): AssessmentDraft {
  if (Date.parse(endedAt) <= Date.parse(startedAt)) throw new Error("结束时间必须晚于开始时间；请检查本机时钟。");
  if (draft.active_intervals.length >= 100) throw new Error("单次审核最多记录 100 个主动工作区间。");
  return { ...draft, active_intervals: [...draft.active_intervals, { started_at: startedAt, ended_at: endedAt }] };
}
export const semanticStatusNames: Record<string, string> = {
  passed: "分项审核通过（人工声明）", failed: "存在未通过项", incomplete: "审核未完整", not_assessed: "尚未分项审核", not_human: "非人工审核记录",
};
