import type { ResearchAssessment, AssessmentSummary, ReviewTarget, CandidateResponse, DatasetResponse, EvidenceResponse, HealthResponse, HypothesisResponse, ResearchTask, RevisionResponse } from "./generated/api-contract";
export type JsonObject = Record<string, unknown>;
export type Evidence = EvidenceResponse;
export type Hypothesis = HypothesisResponse;
export type Candidate = CandidateResponse;
export type Task = ResearchTask;
export type Revision = RevisionResponse;
export interface Research {
  id: string;
  title: string;
  paper_id: string;
  dataset_id: string;
  latest_revision_id: string;
  created_at: string;
  revisions?: Revision[];
  preflight?: { dataset_version: string; fields: string[]; scope: string;
    candidates: { candidate_id: string; status: string; code: string; warmup_rows?: number; missing_fields?: string[]; expression_changed: boolean; suggested_expression?: string }[] };
}
export interface Paper {
  id: string;
  title: string;
  sha256: string;
  pages?: { page: number; text: string }[];
}
export type Dataset = DatasetResponse;
export interface Review {
  id: string;
  run_id: string;
  revision_id: string;
  attempt_id: string;
  candidate_id: string;
  verdict: string;
  category: string;
  note: string;
  result_digest: string;
  created_at: string;
  source?: string;
  assessment?: ResearchAssessment | null;
  assessment_summary?: AssessmentSummary;
}
export interface CandidateState extends Candidate {
  status: string;
  reason?: string;
  attempts?: JsonObject[];
  artifacts?: Record<string, string>;
  result?: {
    metrics: Record<string, number | null>;
    daily?: JsonObject[];
    reason?: string;
    limitations?: string[];
    expression?: string;
    split?: string;
    config?: JsonObject;
    execution?: JsonObject;
    market_metadata?: JsonObject;
    factor_diagnostics?: JsonObject;
  };
  error?: unknown;
}
export interface Run {
  id: string;
  revision_id: string;
  research_id: string;
  status: string;
  mode: string;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  attempt_count: number;
  error?: unknown;
  state?: {
    task?: Partial<Task>;
    error?: unknown;
    candidates?: CandidateState[];
    tool_calls?: number;
    elapsed_seconds?: number;
    events?: JsonObject[];
  };
  verification?: JsonObject | null;
  reviews?: Review[];
  review_targets?: ReviewTarget[];
  attempts?: JsonObject[];
  status_token?: string;
  client_loaded_at?: string;
  regression_target?: {attempt_id:string;result_digest:string}|null;
}
export interface RegressionCase {
  id: string;
  review_id: string;
  research_id: string;
  paper_id: string;
  dataset_id: string;
  candidate_id: string;
  expected_status: string;
  note: string;
  created_at: string;
  version: number;
  contract?: JsonObject | null;
  contract_digest?: string | null;
}
export type RegressionOutcome = "passed" | "failed" | "not_comparable";
export interface RegressionAssertion {
  name: string;
  outcome: RegressionOutcome;
  reason: string;
  expected?: unknown;
  actual?: unknown;
  mismatches?: string[];
  oracle_version?: string;
  formula?: string;
  absolute_tolerance?: number;
  relative_tolerance?: number;
}
export interface RegressionCheck {
  id: string;
  run_id: string;
  created_at: string;
  scope: string;
  passed: boolean;
  outcome?: RegressionOutcome;
  results: {
    case_id: string;
    candidate_id: string;
    expected_status: string;
    actual_status: string | null;
    passed: boolean;
    compatible: boolean;
    reason: string | null;
    outcome?: RegressionOutcome;
    differences?: string[];
    checks?: RegressionAssertion[];
  }[];
}
export interface Artifact {
  attempt_id?: string;
  id: string;
  name: string;
  size: number;
  sha256: string;
}
export type Health = HealthResponse;

export const modes = [
  {
    value: "normalized_fixed",
    label: "规范化固定流程",
    description: "先统一已登记的算子别名，再执行一次。推荐基线。",
  },
  {
    value: "fixed",
    label: "原始固定流程",
    description: "直接执行原始表达式，保留失败，不自动修复。",
  },
  {
    value: "agent",
    label: "有界规则修复",
    description: "遇到已知别名错误时，在预算内修复；不调用 AI。",
  },
] as const;
export const statusNames: Record<string, string> = {
  queued: "排队中",
  running: "计算中",
  cancelling: "取消中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
  evaluated: "已评估",
  blocked: "受阻",
  not_evaluable: "无法评估",
  budget_exhausted: "预算耗尽",
  budget_stopped: "预算耗尽",
  accepted: "接受",
  needs_changes: "需要修改",
  rejected: "拒绝",
  passed: "通过",
  not_comparable: "不可比较",
};
export const activeStatuses = new Set(["queued", "running", "cancelling"]);
export function statusName(status: string) {
  return statusNames[status] ?? status;
}
export function modeName(mode: string) {
  return modes.find((x) => x.value === mode)?.label ?? mode;
}
export function shortId(id?: string) {
  return id ? id.slice(0, 8) : "—";
}
export function formatNumber(value: unknown, digits = 4) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(digits)
    : "—";
}
export function formatDate(value?: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
}
export function describeError(value: unknown): string {
  if (value instanceof Error) return value.message;
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "message" in value)
    return String(value.message);
  return value ? JSON.stringify(value) : "";
}
export function editableTask(task: Task): Task {
  return {
    evidence: task.evidence,
    hypotheses: task.hypotheses,
    candidates: task.candidates,
    evaluation: task.evaluation,
    budget: task.budget,
  };
}
export function parseTask(text: string): Task {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("任务 JSON 格式有误，请检查逗号、引号和括号。");
  }
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("任务必须是一个 JSON 对象。");
  const task = value as Record<string, unknown>;
  for (const key of ["evidence", "hypotheses", "candidates"]) {
    if (!Array.isArray(task[key])) throw new Error(`任务缺少数组字段：${key}`);
  }
  for (const key of ["evaluation", "budget"]) {
    if (!task[key] || typeof task[key] !== "object" || Array.isArray(task[key]))
      throw new Error(`任务缺少对象字段：${key}`);
  }
  const allowed = new Set([
    "evidence",
    "hypotheses",
    "candidates",
    "evaluation",
    "budget",
    "title",
    "schema_version",
  ]);
  const extra = Object.keys(task).filter((key) => !allowed.has(key));
  if (extra.length)
    throw new Error(
      `请移除任务中的路径或未支持字段：${extra.join(", ")}。论文和数据由工作台选择。`,
    );
  return editableTask(task as unknown as Task);
}
export function canReview(run: Run): boolean {
  const v = run.verification;
  return ["completed", "failed"].includes(run.status) && v?.verified === true;
}
export function compatibleCases(
  cases: RegressionCase[],
  research: Research | null,
  candidates: CandidateState[],
): RegressionCase[] {
  if (!research) return [];
  const ids = new Set(candidates.map((c) => c.id));
  return cases.filter(
    (c) =>
      c.research_id === research.id &&
      c.paper_id === research.paper_id &&
      c.dataset_id === research.dataset_id &&
      ids.has(c.candidate_id) &&
      !!c.contract &&
      !!c.contract_digest,
  );
}
// The browser filters identity matches only; the server compares frozen contracts.
export function regressionOutcome(value: {
  passed: boolean;
  outcome?: RegressionOutcome;
}): RegressionOutcome {
  return value.outcome ?? (value.passed ? "passed" : "failed");
}
export function reviewPayload(
  candidateId: string,
  verdict: string,
  category: string,
  note: string,
  source = "human",
) {
  if (!candidateId) throw new Error("请选择要审核的候选。");
  if (!note.trim()) throw new Error("请记录具体审核依据，不能只选择结论。");
  return { candidate_id: candidateId, verdict, category, note: note.trim(), source };
}
