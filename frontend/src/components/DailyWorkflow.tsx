import { activeStatuses, canReview, formatDate, shortId, statusName } from "../domain";
import type { Research, Revision, Run } from "../domain";
import { revisionLabel } from "../dailyWorkflow";

export type DailyStep = "confirm" | "run" | "review" | "report";

export default function DailyWorkflow({ research, revision, run, onStep }: {
  research: Research; revision?: Revision; run: Run | null; onStep: (step: DailyStep) => void;
}) {
  return <section className="panel" aria-label="日常研究流程">
    <h2>日常研究</h2>
    <p>确认候选和论文依据后运行，核对结果并保存审核，再生成报告。流程观测与主动计时均为可选。</p>
    <div className="button-row">
      <button type="button" className="button" onClick={() => onStep("confirm")}>1 · 确认候选</button>
      <button type="button" className="button" disabled={!revision} onClick={() => onStep("run")}>2 · 运行实验</button>
      <button type="button" className="button" disabled={!run || !canReview(run)} onClick={() => onStep("review")}>3 · 审核结果</button>
      <button type="button" className="button" onClick={() => onStep("report")}>4 · 生成报告</button>
    </div>
    <p>待提交修订：{revision ? `v${revision.number}` : "未选择"}。{run ? <>当前实验：{formatDate(run.created_at)} · {shortId(run.id)} · {revisionLabel(research, run.revision_id)} · {statusName(run.status)}。</> : "尚未选择实验；提交后自动打开结果，也可从实验记录选择历史实验。"}</p>
    {run && revision && run.revision_id !== revision.id && <p className="info-note">当前实验绑定 {revisionLabel(research, run.revision_id)}，与待提交的 v{revision.number} 不同。审核和报告使用所选实验的冻结结果；运行新实验使用上方研究版本。</p>}
    {run && activeStatuses.has(run.status) && <p className="fine-print">实验 {shortId(run.id)} 正在处理，结果完成并通过产物校验后可审核。</p>}
  </section>;
}
