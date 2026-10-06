import { readDraft } from "./drafts";
import { assessmentDimensions, emptyAssessment } from "./researchAssessment";
import type { AssessmentDraft } from "./researchAssessment";

export interface ReviewDraft {
  verdict: string; category: string; source: string; note: string; structured: boolean;
  assessment: AssessmentDraft; timerWasRunning: boolean;
}
export const initialReviewDraft = (source = "human", structured = false): ReviewDraft => ({
  verdict: "needs_changes", category: "implementation", source, note: "", structured,
  assessment: emptyAssessment(), timerWasRunning: false,
});
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
export function isReviewDraft(value: unknown): value is ReviewDraft {
  if (!record(value) || !["accepted", "needs_changes", "rejected"].includes(String(value.verdict)) ||
    !["evidence", "hypothesis", "implementation", "data", "evaluation", "other"].includes(String(value.category)) ||
    !["human", "automation", "imported", "legacy_unknown"].includes(String(value.source)) || typeof value.note !== "string" ||
    typeof value.structured !== "boolean" || typeof value.timerWasRunning !== "boolean") return false;
  const a = value.assessment;
  if (!record(a) || typeof a.reviewer !== "string" || !record(a.dimensions) || !Array.isArray(a.active_intervals) || a.active_intervals.length > 100) return false;
  return Object.keys(assessmentDimensions).every((key) => {
    const d = (a.dimensions as Record<string, unknown>)[key];
    return record(d) && ["passed", "failed", "not_assessed", "not_applicable"].includes(String(d.outcome)) && typeof d.reason === "string";
  }) && a.active_intervals.every((i) => record(i) && typeof i.started_at === "string" && typeof i.ended_at === "string" &&
    Number.isFinite(Date.parse(i.started_at)) && Date.parse(i.ended_at) > Date.parse(i.started_at));
}
export const recoveryKey = (workspace: string, kind: string, ...target: string[]) =>
  `paper-alpha:recovery:v1:${JSON.stringify([workspace, kind, ...target])}`;
export function writeDurably(storage: Pick<Storage, "setItem" | "getItem">, key: string, value: unknown) {
  const raw = JSON.stringify(value);
  storage.setItem(key, raw);
  if (storage.getItem(key) !== raw) throw new Error("浏览器未能确认本机保存，请检查存储权限后重试。");
}
export function saveReviewDraft(storage: Pick<Storage, "setItem" | "getItem">, key: string, value: ReviewDraft) {
  writeDurably(storage, key, { version: 1, scope: key, savedAt: new Date().toISOString(), value });
}
export function loadReviewDraft(storage: Pick<Storage, "getItem">, key: string) {
  return readDraft(storage, key, isReviewDraft);
}
export interface PendingMutation {
  version: 1; scope: string; path: string; savedAt: string;
  body: Record<string, unknown> & { idempotency_key: string };
  state: "unconfirmed" | "rejected";
  context?: Record<string, unknown>;
}
export function readMutation(storage: Pick<Storage, "getItem">, key: string, path: string): PendingMutation | null {
  const raw = storage.getItem(key);
  if (!raw) return null;
  const item: unknown = JSON.parse(raw);
  if (!record(item) || item.version !== 1 || item.scope !== key || item.path !== path || typeof item.savedAt !== "string" ||
    !record(item.body) || typeof item.body.idempotency_key !== "string" || !item.body.idempotency_key.trim() ||
    !["unconfirmed", "rejected"].includes(String(item.state))) throw new Error("待确认请求格式或作用域不匹配；为避免重复提交，已阻止新的操作。请保留浏览器数据并检查记录。");
  return item as unknown as PendingMutation;
}
export function stageMutation(storage: Pick<Storage, "getItem" | "setItem">, key: string, path: string, body: Record<string, unknown>, id: string, context?: Record<string, unknown>): PendingMutation {
  if (storage.getItem(key)) throw new Error("已有待确认请求；请先使用原请求安全重试。");
  const item: PendingMutation = { version: 1, scope: key, path, savedAt: new Date().toISOString(),
    body: { ...body, idempotency_key: id }, state: "unconfirmed", ...(context ? { context } : {}) };
  writeDurably(storage, key, item);
  // Return the serialized snapshot, never the caller's mutable object graph.
  return JSON.parse(JSON.stringify(item)) as PendingMutation;
}
export function removeDurably(storage: Pick<Storage, "removeItem" | "getItem">, key: string) {
  storage.removeItem(key);
  if (storage.getItem(key) !== null) throw new Error("浏览器未能清除已处理记录，请检查存储权限。原请求可安全重试。");
}

export function sameMutationRequest(a: PendingMutation | null, b: PendingMutation): boolean {
  return !!a && a.scope === b.scope && a.path === b.path && JSON.stringify(a.body) === JSON.stringify(b.body)
    && JSON.stringify(a.context) === JSON.stringify(b.context);
}

export function rejectMutation(storage: Pick<Storage, "getItem" | "setItem">, key: string, path: string, original: PendingMutation): PendingMutation | null {
  if (!sameMutationRequest(readMutation(storage, key, path), original)) return null;
  const rejected = {...original, state: "rejected" as const};
  writeDurably(storage, key, rejected);
  return rejected;
}

export function acknowledgeMutation(storage: Pick<Storage, "getItem" | "removeItem">, key: string, path: string,
  original: PendingMutation, cleanup?: () => void) {
  const saved = readMutation(storage, key, path);
  if (!saved) return; // Another mounted instance already confirmed it; leave newer unsent drafts alone.
  if (!sameMutationRequest(saved, original)) throw new Error("本机请求记录已改变，请重新加载并检查；没有清除其他请求或草稿。");
  cleanup?.(); // Check ownership before touching the draft; keep pending until all cleanup succeeds.
  removeDurably(storage, key);
}
