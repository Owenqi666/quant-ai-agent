import { ApiError } from "./api";
import type { Research, Run } from "./domain";
import type { DailyStep } from "./components/DailyWorkflow";
import { translate } from "./i18n";

/** Only navigation identifiers are stored. Business data and drafts stay elsewhere. */
export interface WorkPosition {
  stage: DailyStep;
  revisionId: string;
  runId: string;
  issueId?: string;
  candidateId?: string;
}
export interface StoredWorkPosition {
  version: 1;
  workspaceId: string;
  researchId: string;
  position: WorkPosition;
}
export type WorkPositionNotice =
  | "storage-unavailable" | "invalid-record" | "scope-mismatch"
  | "revision-missing" | "run-missing" | "run-mismatch"
  | "candidate-missing" | "issue-missing" | "issue-mismatch" | "read-failed";
export type PositionStorage = Pick<Storage, "getItem" | "setItem">;
export interface NavigationIssue { id: string; research_id: string; candidate_id: string }
export interface WorkPositionResolution {
  status: "absent" | "restored" | "degraded" | "unavailable" | "stale";
  position: WorkPosition | null;
  run: Run | null;
  notices: WorkPositionNotice[];
  error?: unknown;
}
export const workPositionKey = (workspaceId: string, researchId: string) =>
  `paper-alpha:work-position:v1:${JSON.stringify([workspaceId, researchId])}`;
const stages = new Set<DailyStep>(["materials", "evidence", "candidates", "experiments", "review", "report"]);
const identifier = (value: unknown): value is string => typeof value === "string" && value.length <= 512;
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);

function copyPosition(value: unknown): WorkPosition | null {
  if (!object(value) || !stages.has(value.stage as DailyStep) || !identifier(value.revisionId) || !identifier(value.runId)) return null;
  if (value.issueId !== undefined && !identifier(value.issueId)) return null;
  if (value.candidateId !== undefined && !identifier(value.candidateId)) return null;
  return { stage: value.stage as DailyStep, revisionId: value.revisionId, runId: value.runId,
    ...(value.issueId ? { issueId: value.issueId } : {}), ...(value.candidateId ? { candidateId: value.candidateId } : {}) };
}

export function readWorkPosition(storage: Pick<Storage, "getItem"> | null, workspaceId: string, researchId: string): {
  record: StoredWorkPosition | null; notices: WorkPositionNotice[];
} {
  if (!storage) return { record: null, notices: ["storage-unavailable"] };
  let text: string | null;
  try { text = storage.getItem(workPositionKey(workspaceId, researchId)); }
  catch { return { record: null, notices: ["storage-unavailable"] }; }
  if (text === null) return { record: null, notices: [] };
  try {
    const value: unknown = JSON.parse(text);
    if (!object(value) || value.version !== 1) return { record: null, notices: ["invalid-record"] };
    if (value.workspaceId !== workspaceId || value.researchId !== researchId) return { record: null, notices: ["scope-mismatch"] };
    const position = copyPosition(value.position);
    return position ? { record: { version: 1, workspaceId, researchId, position }, notices: [] }
      : { record: null, notices: ["invalid-record"] };
  } catch { return { record: null, notices: ["invalid-record"] }; }
}
export function saveWorkPosition(storage: Pick<Storage, "setItem"> | null, workspaceId: string, researchId: string, position: WorkPosition): {
  saved: boolean; notices: WorkPositionNotice[];
} {
  const clean = copyPosition(position);
  if (!workspaceId || !researchId || !clean) return { saved: false, notices: ["invalid-record"] };
  if (!storage) return { saved: false, notices: ["storage-unavailable"] };
  try {
    const record: StoredWorkPosition = { version: 1, workspaceId, researchId, position: clean };
    storage.setItem(workPositionKey(workspaceId, researchId), JSON.stringify(record));
    return { saved: true, notices: [] };
  } catch { return { saved: false, notices: ["storage-unavailable"] }; }
}
export function workPositionNoticeMessage(notice: WorkPositionNotice): string {
  const messages: Record<WorkPositionNotice, [string, string]> = {
    "storage-unavailable": ["浏览器无法保存工作位置；当前仍可操作，刷新后需重新选择。审核草稿的保存状态请另行确认。", "This browser cannot persist your work position. You can continue, but must select it again after refresh. Check review-draft storage separately."],
    "invalid-record": ["已保存的工作位置无法读取，未应用；请选择研究阶段和实验。", "The saved work position is unreadable and was not applied. Select a stage and experiment."],
    "scope-mismatch": ["工作位置不属于当前工作区或研究，未应用。", "The saved position does not belong to this workspace or study and was not applied."],
    "revision-missing": ["原编辑版本已不可用，已返回当前研究的最新可用版本；实验仍按其冻结版本读取。", "The editing version is unavailable. The latest available study version is selected; experiments retain their frozen version."],
    "run-missing": ["原先选择的实验已不存在，未选择其他实验。请选择记录或继续查看材料。", "The selected experiment no longer exists. No other experiment was selected. Choose a record or inspect the materials."],
    "run-mismatch": ["原实验的身份或研究归属不匹配，未恢复，也未改选其他实验。", "The experiment identity or study does not match. It was not restored and no alternative was selected."],
    "candidate-missing": ["原候选不在当前冻结目标中，未恢复候选选择。", "The candidate is not in the current frozen target. Its selection was not restored."],
    "issue-missing": ["原问题记录已不可用，未恢复问题选择。", "The original issue is unavailable. Its selection was not restored."],
    "issue-mismatch": ["原问题的研究或候选归属不匹配，未恢复问题选择。", "The issue does not match the study or candidate. Its selection was not restored."],
    "read-failed": ["暂时无法恢复原工作位置；原记录保留，未改选其他实验。请重试或明确选择新的工作位置。", "The original work position could not be restored. It is retained and no other experiment was selected. Retry or explicitly choose a new position."],
  };
  return translate(...messages[notice]);
}
const missing = (error: unknown) => error instanceof ApiError && error.status === 404 && !error.contractViolation;
const stale = (): WorkPositionResolution => ({ status: "stale", position: null, run: null, notices: [] });

export interface RestoreWorkPositionOptions {
  workspaceId: string;
  researchId: string;
  research: Research;
  record: StoredWorkPosition | null;
  notices?: WorkPositionNotice[];
  lookupRun: (id: string) => Promise<Run>;
  lookupIssue?: (id: string) => Promise<NavigationIssue>;
  isCurrent?: () => boolean;
}

/** A historical run is always read by identity, independently of a bounded catalog. */
export async function resolveWorkPosition(options: RestoreWorkPositionOptions): Promise<WorkPositionResolution> {
  const { workspaceId, researchId, research, record, lookupRun, lookupIssue } = options;
  const current = options.isCurrent || (() => true);
  const notices = [...(options.notices || [])];
  if (!current()) return stale();
  if (research.id !== researchId || record && (record.workspaceId !== workspaceId || record.researchId !== researchId)) {
    return { status: "unavailable", position: null, run: null, notices: [...notices, "scope-mismatch"] };
  }
  if (!record) return { status: "absent", position: null, run: null, notices };
  const position = { ...record.position };
  const revisions = (research.revisions || []).filter(revision => revision.research_id === researchId);
  const latest = revisions.find(r => r.id === research.latest_revision_id) || revisions.at(-1);
  if (!revisions.some(r => r.id === position.revisionId)) {
    position.revisionId = latest?.id || "";
    notices.push("revision-missing");
  }
  let run: Run | null = null;
  try {
    if (position.runId) {
      try { run = await lookupRun(position.runId); }
      catch (error) {
        if (!current()) return stale();
        if (!missing(error)) throw error;
        notices.push("run-missing"); position.runId = "";
      }
      if (!current()) return stale();
      if (run && (run.id !== position.runId || run.research_id !== researchId || !revisions.some(r => r.id === run!.revision_id))) {
        notices.push("run-mismatch"); run = null; position.runId = "";
      }
    }
    const targetRevisionId = run?.revision_id || position.revisionId;
    const targetCandidates = run?.state?.task?.candidates || run?.state?.candidates
      || revisions.find(r => r.id === targetRevisionId)?.task?.candidates || [];
    if (position.candidateId && (!position.runId && record.position.runId || !targetCandidates.some(c => c.id === position.candidateId))) {
      delete position.candidateId; notices.push("candidate-missing");
    }
    if (position.issueId) {
      if (!lookupIssue) { delete position.issueId; notices.push("issue-missing"); }
      else {
        let issue: NavigationIssue | null = null;
        try { issue = await lookupIssue(position.issueId); }
        catch (error) {
          if (!current()) return stale();
          if (!missing(error)) throw error;
          delete position.issueId; notices.push("issue-missing");
        }
        if (!current()) return stale();
        if (issue && (issue.id !== position.issueId || issue.research_id !== researchId || position.candidateId && issue.candidate_id !== position.candidateId)) {
          delete position.issueId; notices.push("issue-mismatch");
        }
      }
    }
    return current() ? { status: notices.length ? "degraded" : "restored", position, run, notices } : stale();
  } catch (error) {
    return current() ? { status: "unavailable", position: null, run: null, notices: [...notices, "read-failed"], error } : stale();
  }
}

/** Cancelling or changing workspace/selection invalidates an unfinished restore. */
export class WorkPositionRequest {
  private sequence = 0;
  cancel() { this.sequence += 1; }
  begin(isScopeCurrent: () => boolean) {
    if (!isScopeCurrent()) return () => false;
    const sequence = ++this.sequence;
    return () => sequence === this.sequence && isScopeCurrent();
  }
}
