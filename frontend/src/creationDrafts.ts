import { draftKey, isResearchDraft, readDraft } from "./drafts";
import type { Draft, ResearchDraft } from "./drafts";
import { readMutation, recoveryKey, removeDurably } from "./reviewRecovery";
import type { PendingMutation } from "./reviewRecovery";
import type { PresetSelection } from "./evaluationPresets";
import { creationFlowMutationKey, readCreationFlowMutation, isResearchCreationFlow } from "./researchCreationFlow";
import type { ResearchCreationFlowState } from "./researchCreationFlow";
import { creationDraftKey } from "./submissionRecovery";

export const creationStages = ["materials", "evidence", "candidates", "experiment"] as const;
export type CreationStage = typeof creationStages[number];
export interface CreationDraft extends ResearchDraft {
  creationStage?: CreationStage;
  creationMode?: "manual" | "ai";
  creationFlow?: ResearchCreationFlowState;
  creationPreset?: PresetSelection;
}
export interface CreationDraftEntry { key: string; scope: string; draft: Draft<CreationDraft>; aiPending: PendingMutation | null; aiRecoveryError: string | null }
export interface CreationDraftDiscovery {
  entries: CreationDraftEntry[];
  pending: PendingMutation | null;
  pendingScope: string | null;
  errors: string[];
  recoveryBlocked: boolean;
}
type ReadStorage = Pick<Storage, "length" | "key" | "getItem">;
export function isCreationDraft(value: unknown): value is CreationDraft {
  if (!isResearchDraft(value)) return false;
  const item = value as CreationDraft;
  return (item.creationStage === undefined || creationStages.includes(item.creationStage)) &&
    (item.creationMode === undefined || item.creationMode === "manual" || item.creationMode === "ai") &&
    (item.creationFlow === undefined || isResearchCreationFlow(item.creationFlow)) &&
    (item.creationPreset === undefined || (!!item.creationPreset && ["presetId", "configurationDigest", "sourceDigest", "datasetId", "datasetSha256", "confirmationKey"].every(key => typeof (item.creationPreset as unknown as Record<string, unknown>)[key] === "string")));
}
export function creationStage(value: CreationDraft): CreationStage { return value.creationStage ?? "materials"; }
export function creationScope(workspaceId: string, key: string): string {
  const prefix = draftKey(workspaceId, "create:");
  if (!workspaceId || !key.startsWith(prefix) || key.length === prefix.length) throw new Error("创建草稿不属于当前工作区。");
  return key.slice(prefix.length);
}
export function discoverCreationDrafts(storage: ReadStorage, workspaceId: string): CreationDraftDiscovery {
  const result: CreationDraftDiscovery = { entries: [], pending: null, pendingScope: null, errors: [], recoveryBlocked: false };
  if (!workspaceId) return result;
  try {
    const pending = readMutation(storage, recoveryKey(workspaceId, "research-request"), "/researches");
    if (pending) {
      result.pending = pending;
      result.pendingScope = creationScope(workspaceId, creationDraftKey(workspaceId, pending.context));
    }
  } catch { result.recoveryBlocked = true; result.errors.push("研究创建恢复记录不可读取；已保留记录，请先检查，避免重复创建。"); }
  const prefix = draftKey(workspaceId, "create:");
  try {
    const keys: string[] = [];
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index);
      if (key?.startsWith(prefix)) keys.push(key);
    }
    for (const key of keys) {
      try {
        const scope = creationScope(workspaceId, key), draft = readDraft(storage, key, isCreationDraft);
        if (draft && Number.isFinite(Date.parse(draft.savedAt))) {
          let aiPending: PendingMutation | null = null, aiRecoveryError: string | null = null;
          try { aiPending = readCreationFlowMutation(storage, creationFlowMutationKey(workspaceId, key)); }
          catch { aiRecoveryError = "此草稿的 AI 请求恢复记录损坏；已保留草稿和收据，请检查原记录。"; }
          result.entries.push({ key, scope, draft, aiPending, aiRecoveryError });
        }
        else if (draft) throw new Error("Invalid timestamp");
      } catch { result.errors.push(`有一份创建草稿无法读取，已保留原记录：${key.slice(prefix.length)}`); }
    }
  } catch { result.errors.push("无法读取浏览器创建草稿；请检查存储权限。服务器记录未改变。"); }
  result.entries.sort((a, b) => b.draft.savedAt.localeCompare(a.draft.savedAt) || a.scope.localeCompare(b.scope));
  return result;
}
/** Recheck the receipt at click time; a newly frozen submission must not lose its draft. */
export function discardCreationDraft(storage: ReadStorage & Pick<Storage, "removeItem">, workspaceId: string, entry: CreationDraftEntry): void {
  creationScope(workspaceId, entry.key);
  const discovered = discoverCreationDrafts(storage, workspaceId);
  if (discovered.recoveryBlocked || discovered.pendingScope === entry.scope) throw new Error("此草稿关联待确认创建请求；请先确认原请求，不能丢弃其恢复记录。");
  const aiPending = readCreationFlowMutation(storage, creationFlowMutationKey(workspaceId, entry.key));
  if (aiPending) throw new Error("此草稿关联待确认 AI 请求；请恢复并确认原请求，不能丢弃草稿。");
  const current = readDraft(storage, entry.key, isCreationDraft);
  if (!current) return;
  if (JSON.stringify(current) !== JSON.stringify(entry.draft)) throw new Error("此草稿已在另一窗口更新，请刷新列表后再决定是否丢弃。");
  removeDurably(storage, entry.key);
}
