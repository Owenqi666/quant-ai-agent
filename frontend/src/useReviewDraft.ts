import { useEffect, useState } from "react";
import { describeError } from "./domain";
import { loadReviewDraft, removeDurably, saveReviewDraft } from "./reviewRecovery";
import type { ReviewDraft } from "./reviewRecovery";

/** The editor is keyed by complete frozen output identity. */
export function useReviewDraft(key: string, initial: ReviewDraft) {
  const [value, setValue] = useState(initial);
  const [offered, setOffered] = useState<ReviewDraft | null>(null);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [interrupted, setInterrupted] = useState(false);
  const [unreadable, setUnreadable] = useState(false);
  useEffect(() => {
    try { setOffered(loadReviewDraft(localStorage, key)?.value || null); }
    catch (e) { setError(`草稿不可读取，未应用。${describeError(e)}`); setUnreadable(true); }
    setReady(true);
  }, [key]);
  function update(next: ReviewDraft) {
    setValue(next);
    try { saveReviewDraft(localStorage, key, next); setError(""); }
    catch (e) { setError(`浏览器无法保存审核草稿；刷新前请另行保留内容。${describeError(e)}`); }
  }
  function clear() {
    try { removeDurably(localStorage, key); setOffered(null); setUnreadable(false); setError(""); setValue(initial); }
    catch (e) { setError(`本机草稿未清除。${describeError(e)}`); }
  }
  function restore() {
    if (!offered) return;
    setInterrupted(offered.timerWasRunning);
    const next = { ...offered, timerWasRunning: false };
    setOffered(null); update(next);
  }
  return { value, update, offered, error, ready, interrupted, unreadable, restore, clear };
}
