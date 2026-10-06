import { draftKey } from "./drafts";

/** Only the draft frozen with a creation request may be cleared on confirmation. */
export function creationDraftKey(workspaceId: string, context: Record<string, unknown> | undefined): string {
  const key = context?.draft_key;
  if (!workspaceId || typeof key !== "string" || !key.startsWith(draftKey(workspaceId, "create:")))
    throw new Error("待确认创建请求缺少原工作区草稿标识，请保留本机记录并检查；没有清除其他草稿。");
  return key;
}
