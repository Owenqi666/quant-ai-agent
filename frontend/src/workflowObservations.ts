import { recoveryKey, removeDurably, writeDurably } from "./reviewRecovery";

export const observationSources = { human: "本人实际观测", automation: "自动化验收样例" } as const;
export type ObservationSource = keyof typeof observationSources;
export const observationPhases: Record<string, string> = {
  reading: "阅读", hypothesis: "假设", implementation: "实现", run_setup: "配置与运行", review: "审核", report: "报告",
};
export const MAX_OBSERVATION_BYTES = 256 * 1024;
export interface ObservationDraft { raw: string; source: ObservationSource | "" }
const isObject = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const isSource = (value: unknown): value is ObservationSource | "" => value === "" || value === "human" || value === "automation";

/** JSON.parse has already checked syntax; inspect tokens before accepting its lossy result. */
function rejectAmbiguousJson(raw: string) {
  const tokens = raw.match(/"(?:\\.|[^"\\])*"|[{}\[\],:]|true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/g) || [];
  let cursor = 0;
  function value(depth: number) {
    if (depth > 64) throw new Error("观测 JSON 嵌套不可超过 64 层。");
    const token = tokens[cursor++];
    if (token === "{") {
      const keys = new Set<string>();
      while (tokens[cursor] !== "}") {
        const key = JSON.parse(tokens[cursor++]) as string;
        if (keys.has(key)) throw new Error(`观测 JSON 包含重复字段 ${key}，请消除歧义后预检。`);
        keys.add(key); cursor++; value(depth + 1);
        if (tokens[cursor] === ",") cursor++;
      }
      cursor++;
    } else if (token === "[") {
      while (tokens[cursor] !== "]") { value(depth + 1); if (tokens[cursor] === ",") cursor++; }
      cursor++;
    } else if (/^-?\d/.test(token) && !Number.isFinite(Number(token))) throw new Error("观测 JSON 包含无法有限表示的数字。");
  }
  value(0);
}
export function observationPath(researchId: string) {
  if (!researchId) throw new Error("请先选择研究。");
  return `/researches/${encodeURIComponent(researchId)}/workflow-observations`;
}
export const observationScopeKey = (workspaceId: string, researchId: string, kind: "draft" | "import") =>
  recoveryKey(workspaceId, `workflow-observation-${kind}`, researchId);

/** Parse only the envelope here. Timing, identity and completeness belong to the server. */
export function parseObservation(raw: string, source: ObservationSource | ""): Record<string, unknown> {
  if (!raw.trim()) throw new Error("请上传或填写观测 JSON。");
  if (new TextEncoder().encode(raw).length > MAX_OBSERVATION_BYTES) throw new Error("观测 JSON 不可超过 256 KiB。");
  let value: unknown;
  try { value = JSON.parse(raw); } catch { throw new Error("观测 JSON 格式不正确，请检查括号、引号和逗号。"); }
  rejectAmbiguousJson(raw);
  if (!isObject(value)) throw new Error("观测 JSON 顶层必须是对象。");
  if (!source) throw new Error("请明确声明这是本人实际观测还是自动化验收样例。");
  if (value.source !== source) throw new Error("JSON 的 source 与当前声明不一致；请核对来源后手动修改，系统不会替换来源。");
  if (value.record_status !== "observed") throw new Error("空白或待填写模板不是观测记录；完成实际记录后才能预检导入。");
  return value;
}

export function saveObservationDraft(storage: Pick<Storage, "setItem" | "getItem">, key: string, draft: ObservationDraft) {
  writeDurably(storage, key, { version: 1, scope: key, draft });
}
export function loadObservationDraft(storage: Pick<Storage, "getItem">, key: string): ObservationDraft | null {
  const raw = storage.getItem(key);
  if (raw === null) return null;
  const value: unknown = JSON.parse(raw);
  if (!isObject(value) || value.version !== 1 || value.scope !== key || !isObject(value.draft) ||
      typeof value.draft.raw !== "string" || !isSource(value.draft.source)) throw new Error("观测草稿格式或作用域不匹配，请保留本机数据并检查。");
  return { raw: value.draft.raw, source: value.draft.source };
}
/** A late successful POST may clear only its own submitted draft, never another tab's edits. */
export function clearSubmittedObservationDraft(storage: Pick<Storage, "getItem" | "removeItem">, key: string, expected: ObservationDraft) {
  const current = loadObservationDraft(storage, key);
  if (current && current.raw === expected.raw && current.source === expected.source) removeDurably(storage, key);
}

/** Separate epochs are used for file reads, validation, list, summary and detail. */
export class ObservationRequestEpoch {
  private generation = 0;
  begin() { return ++this.generation; }
  invalidate() { this.generation += 1; }
  isCurrent(token: number) { return token === this.generation; }
}

export function observedSeconds(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)} 秒` : "未测量";
}
