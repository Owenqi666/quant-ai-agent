import { useI18n } from "../i18n";
import { formatDate } from "../domain";
import type { Draft, ResearchDraft } from "../drafts";

export default function DraftNotice({ pending, error, enabled, onRestore, onDiscard }: {
  pending: Draft<ResearchDraft> | null; error: string; enabled: boolean; onRestore: () => void; onDiscard: () => void;
}) {
  const {t} = useI18n();
  return <>
    {pending && <div className="info-note warning" role="alert">
      <p>{t(`发现此工作区和编辑起点的未提交草稿（${formatDate(pending.savedAt)}）。草稿尚未应用。`, `An unsaved draft exists for this workspace and revision (${formatDate(pending.savedAt)}). It has not been applied.`)}</p>
      <p>{pending.value.title || t("未命名研究", "Untitled study")}{t("；恢复后请重新确认论文、数据版本与时间切分。", "; after restoring, check the paper, data version and time splits.")}</p>
      <div className="button-row"><button type="button" className="button" onClick={onRestore}>{t("恢复此草稿", "Restore this draft")}</button><button type="button" className="button" onClick={onDiscard}>{t("丢弃此草稿", "Discard this draft")}</button></div>
    </div>}
    {error && <p className="info-note warning" role="alert">{error}</p>}
    <p className="fine-print">{enabled ? t("编辑内容仅保存在当前浏览器的此工作区草稿中；提交成功后清除。草稿不代表已保存研究或审核结论。", "Edits are stored as a draft in this browser and workspace, then cleared after successful submission. A draft is not a saved study or review.") : t("工作区身份尚未确认，暂不保存浏览器草稿。", "The workspace is not confirmed yet. Browser draft storage is paused.")}</p>
  </>;
}
