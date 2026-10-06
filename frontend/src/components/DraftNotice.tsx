import { formatDate } from "../domain";
import type { Draft, ResearchDraft } from "../drafts";

export default function DraftNotice({ pending, error, enabled, onRestore, onDiscard }: {
  pending: Draft<ResearchDraft> | null; error: string; enabled: boolean; onRestore: () => void; onDiscard: () => void;
}) {
  return <>
    {pending && <div className="info-note warning" role="alert">
      <p>发现此工作区和编辑起点的未提交草稿（{formatDate(pending.savedAt)}）。草稿尚未应用。</p>
      <p>{pending.value.title || "未命名研究"}；恢复后请重新确认论文、数据版本与时间切分。</p>
      <div className="button-row"><button type="button" className="button" onClick={onRestore}>恢复此草稿</button><button type="button" className="button" onClick={onDiscard}>丢弃此草稿</button></div>
    </div>}
    {error && <p className="info-note warning" role="alert">{error}</p>}
    <p className="fine-print">{enabled ? "编辑内容仅保存在当前浏览器的此工作区草稿中；提交成功后清除。草稿不代表已保存研究或审核结论。" : "工作区身份尚未确认，暂不保存浏览器草稿。"}</p>
  </>;
}
