import { readMutation, removeDurably } from "./reviewRecovery";

/** Clear only the exact draft bytes which accompanied this frozen request. */
export function clearSubmittedDraft(storage:Pick<Storage,"getItem"|"removeItem">, expectedKey:string, context?:Record<string,unknown>) {
  if(typeof context?.draftKey !== "string" || typeof context.draftRaw !== "string")return;
  if(context.draftKey!==expectedKey)throw new Error("原草稿作用域不匹配，未清理任何草稿。");
  if(storage.getItem(context.draftKey)===context.draftRaw)removeDurably(storage,context.draftKey);
}

/** An unresolved issue decision owns its original route even after navigation. */
export function issueDecisionPath(storage:Pick<Storage,"getItem">, key:string, issueId:string):string {
  const fallback=`/issues/${encodeURIComponent(issueId || "unselected")}/events`;
  try {
    const raw=storage.getItem(key);
    if(!raw)return fallback;
    const parsed=JSON.parse(raw);
    if(typeof parsed?.path!=="string" || !/^\/issues\/[^/]+\/events$/.test(parsed.path))return fallback;
    const pending=readMutation(storage,key,parsed.path);
    if(!pending || typeof pending.context?.issueId!=="string" || parsed.path!==`/issues/${encodeURIComponent(pending.context.issueId)}/events`)return fallback;
    return parsed.path;
  } catch {return fallback;} // The mutation hook then exposes the invalid record and blocks new sends.
}
