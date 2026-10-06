import { useEffect, useState } from "react";

export interface Draft<T> { version: 1; scope: string; savedAt: string; value: T }
export const draftKey = (workspace: string, scope: string) => `paper-alpha:draft:v1:${workspace}:${scope}`;
export function readDraft<T>(storage: Pick<Storage, "getItem">, key: string, validate: (value: unknown) => value is T): Draft<T> | null {
  const raw = storage.getItem(key);
  if (!raw) return null;
  const draft = JSON.parse(raw) as Draft<unknown>;
  if (draft.version !== 1 || draft.scope !== key || typeof draft.savedAt !== "string" || !validate(draft.value)) throw new Error("草稿格式或工作区不匹配，未自动应用。");
  return draft as Draft<T>;
}

/** Existing drafts are offered explicitly; never applied or overwritten on mount. */
export function useDraft<T>(key: string | null, initial: T, validate: (value: unknown) => value is T) {
  const [value, setValue] = useState(initial);
  const [pending, setPending] = useState<Draft<T> | null>(null);
  const [ready, setReady] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!key) { setReady(true); return; }
    try { setPending(readDraft(localStorage, key, validate)); }
    catch { setError("浏览器草稿不可读取；不会自动覆盖。可继续编辑，另行保留任务 JSON。"); }
    setReady(true);
    // The parent keys this component by immutable workspace/research/base revision.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  useEffect(() => {
    if (!key || !ready || pending || !dirty) return;
    try { localStorage.setItem(key, JSON.stringify({ version: 1, scope: key, savedAt: new Date().toISOString(), value })); setError(""); }
    catch { setError("浏览器无法保存草稿；刷新前请复制高级任务 JSON。"); }
  }, [key, ready, pending, dirty, value]);
  const update = (next: T | ((previous: T) => T)) => {
    setValue((previous) => typeof next === "function" ? (next as (value: T) => T)(previous) : next);
    setDirty(true);
  };
  const clear = () => {
    if (key) { try { localStorage.removeItem(key); } catch { setError("浏览器草稿未能清除。再次打开时请丢弃旧草稿。"); } }
    setPending(null); setDirty(false);
  };
  return { value, update, pending, error, dirty, ready,
    restore: () => { if (pending) { setValue(pending.value); setPending(null); setDirty(true); } },
    discard: () => { clear(); setValue(initial); }, clear };
}

export interface ResearchDraft { title: string; paperId: string; datasetId: string; taskText: string; note: string }
export const isResearchDraft = (value: unknown): value is ResearchDraft => !!value && typeof value === "object" &&
  ["title", "paperId", "datasetId", "taskText", "note"].every((key) => typeof (value as Record<string, unknown>)[key] === "string");
