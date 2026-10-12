import { useCallback, useEffect, useState } from "react";
import { CREATION_DRAFT_CHANGED, notifyDraftChanged } from "../drafts";
import { creationStage, discoverCreationDrafts, discardCreationDraft } from "../creationDrafts";
import type { CreationDraftDiscovery, CreationDraftEntry } from "../creationDrafts";
import { describeError, formatDate } from "../domain";
import { useI18n } from "../i18n";

export default function CreationDraftInbox({ workspaceId, disabled = false, onResume, onRecoverRequest }: {
  workspaceId: string;
  disabled?: boolean;
  onResume: (entry: CreationDraftEntry) => void;
  onRecoverRequest: (scope: string) => void;
}) {
  const { t } = useI18n();
  const [state, setState] = useState<CreationDraftDiscovery>({ entries: [], pending: null, pendingScope: null, errors: [], recoveryBlocked: false });
  const [error, setError] = useState("");
  const refresh = useCallback(() => {
    try { setState(discoverCreationDrafts(localStorage, workspaceId)); }
    catch { setState({ entries: [], pending: null, pendingScope: null, errors: [t("浏览器草稿暂不可读取。", "Browser drafts are unavailable.")], recoveryBlocked: true }); }
  }, [workspaceId, t]);
  useEffect(() => {
    refresh();
    window.addEventListener("storage", refresh);
    window.addEventListener(CREATION_DRAFT_CHANGED, refresh);
    window.addEventListener("focus", refresh);
    return () => { window.removeEventListener("storage", refresh); window.removeEventListener(CREATION_DRAFT_CHANGED, refresh); window.removeEventListener("focus", refresh); };
  }, [refresh]);
  if (!workspaceId || (!state.entries.length && !state.pending && !state.errors.length)) return null;
  const labels = { materials: t("材料输入", "Materials"), evidence: t("证据与假说", "Evidence and hypotheses"), candidates: t("候选", "Candidates"), experiment: t("实验配置", "Experiment configuration") };
  return <section className="info-note" aria-label={t("未提交研究", "Unsubmitted studies")}>
    <h3>{t("继续未提交研究", "Continue an unsubmitted study")}</h3>
    <p>{t("以下为当前浏览器的草稿。恢复不会创建研究或运行实验；恢复后请重新核对数据和时间设置。", "These drafts belong to this browser. Restoring creates no study or experiment; check the data and time settings again.")}</p>
    {state.pending && <p role="status">{t("有一个创建请求需要确认。请先恢复原请求，避免重复创建。", "A creation request needs confirmation. Recover the original request to avoid duplicates.")} {state.pendingScope && <button className="button small" disabled={disabled || state.recoveryBlocked} onClick={() => onRecoverRequest(state.pendingScope!)}>{t("确认原创建请求", "Confirm original creation request")}</button>}</p>}
    {[...state.errors, ...(error ? [error] : [])].map((message, index) => <p role="alert" key={index}>{message}</p>)}
    {state.entries.map(entry => <div className="button-row" key={entry.key}>
      <span>{entry.draft.value.title || t("未命名研究", "Untitled study")} · {labels[creationStage(entry.draft.value)]} · {formatDate(entry.draft.savedAt)}</span>{entry.aiRecoveryError && <p role="alert">{entry.aiRecoveryError}</p>}
      <button className="button small" disabled={disabled || state.recoveryBlocked || !!state.pending} onClick={() => onResume(entry)}>{entry.aiPending ? t("恢复 AI 原请求", "Recover original AI request") : t("恢复草稿", "Restore draft")}</button>
      <button className="button small" disabled={disabled || state.recoveryBlocked || state.pendingScope === entry.scope || !!entry.aiPending || !!entry.aiRecoveryError} onClick={() => {
        try { discardCreationDraft(localStorage, workspaceId, entry); setError(""); notifyDraftChanged(entry.key); refresh(); }
        catch (cause) { setError(describeError(cause)); refresh(); }
      }}>{t("丢弃草稿", "Discard draft")}</button>
    </div>)}
  </section>;
}
