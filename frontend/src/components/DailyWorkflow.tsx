import { activeStatuses, canReview, shortId, statusName } from "../domain";
import type { Research, Revision, Run } from "../domain";
import { useI18n } from "../i18n";
import "./researchStages.css";

export type DailyStep = "materials" | "evidence" | "candidates" | "experiments" | "review" | "report";
export type ResearchStage = "materials" | "evidence" | "candidates";

export default function DailyWorkflow({ research, revision, run, stage, editing, onStep, researchRuns = [], onOpenRun }: {
  research: Research; revision?: Revision; run: Run | null; stage: DailyStep; editing: boolean; onStep: (step: DailyStep) => void;
  researchRuns?: Run[]; onOpenRun?: (runId: string) => void;
}) {
  const { locale, t } = useI18n();
  const steps = [
    {id: "materials", label: t("材料输入", "Material")}, {id: "evidence", label: t("证据与假说", "Evidence")},
    {id: "candidates", label: t("候选", "Candidates")}, {id: "experiments", label: t("实验", "Experiments")}, {id: "review", label: t("结果审核", "Result review")},
  ] as const;
  const runStatus = run ? statusName(run.status) : "";
  const englishStatus: Record<string, string> = { queued: "Queued", running: "Running", cancelling: "Cancelling", completed: "Completed", failed: "Failed", interrupted: "Interrupted", cancelled: "Cancelled" };
  const statusLabel = run ? t(runStatus, englishStatus[run.status] || run.status) : "";

  const availableRuns = researchRuns.filter(item => item.research_id === research.id && ["completed", "failed"].includes(item.status))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const recentRun = availableRuns[0];
  const status = [t("已登记", "Registered"), t(`${revision?.task?.hypotheses.length ?? 0} 条假说`, `${revision?.task?.hypotheses.length ?? 0} hypotheses`),
    t(`${revision?.task?.candidates.length ?? 0} 个候选`, `${revision?.task?.candidates.length ?? 0} candidates`), run ? statusLabel : t("尚未选择实验", "No experiment selected"),
    run?.reviews?.length ? t(`已有 ${run.reviews.length} 条判断`, `${run.reviews.length} reviews saved`) : run && canReview(run) ? t("可查看并判断", "Ready for inspection and review") : !run && recentRun ? t("已有实验可查看，尚未选择", "Past experiments available; none selected") : t("等待可核验结果", "Awaiting verifiable results")];
  const boundRevision = run ? research.revisions?.find(item => item.id === run.revision_id) : undefined;
  const resultVersion = boundRevision ? `v${boundRevision.number}` : t(`修订 ${shortId(run?.revision_id)}`, `Revision ${shortId(run?.revision_id)}`);
  const createdAt = run ? new Date(run.created_at).toLocaleString(locale === "zh" ? "zh-CN" : "en-GB", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "";
  return <section className="panel research-flow daily-research-flow" aria-label={t("日常研究流程", "Daily research workflow")}>
    <h2 className="rs-sr-only">{t("研究流程", "Research workflow")}</h2>
    <nav className="flow-steps" aria-label={t("研究阶段", "Research stages")}>
      {steps.map((step, index) => <button type="button" key={step.id}
        className={`flow-step ${stage === step.id ? "active" : ""}`} aria-current={stage === step.id ? "step" : undefined}
        onClick={() => onStep(step.id)}>
        <span>{index + 1} · {step.label}</span><small>{status[index]}</small>
      </button>)}
    </nav>
    <p className="flow-identity"><strong>{t("待提交修订：", "Version for submission: ")}{revision ? `v${revision.number}` : t("未选择", "Not selected")}</strong><span>{run ? <>{t("当前实验：", "Selected experiment: ")}{createdAt} · {shortId(run.id)} · {resultVersion} · {statusLabel}</> : t("尚未选择实验；进入实验阶段可提交或选取历史记录。", "No experiment selected. Open Experiments to submit or select a historical run.")}</span></p>
    {!run && recentRun && <p className="flow-context-note">
      <span>{t(`已有 ${availableRuns.length} 个已结束实验可查看。请选择记录核对冻结结果；不会自动提交新实验。`, `${availableRuns.length} finished experiments are available. Select a record to inspect its frozen output; no new experiment is submitted automatically.`)}</span>{" "}
      {onOpenRun && <button type="button" className="button small" onClick={() => onOpenRun(recentRun.id)}>{t("查看已有实验", "View existing experiment")} · {shortId(recentRun.id)}</button>}
    </p>}
    {run && revision && run.revision_id !== revision.id && <p className="flow-context-note">{t(`当前实验绑定 ${resultVersion}，与待提交的 v${revision.number} 不同。审核和报告使用所选实验的冻结结果；运行新实验使用上方研究版本。`, `The selected experiment belongs to ${resultVersion}, while v${revision.number} is selected for submission. Review and reports use the selected experiment’s frozen results; a new experiment uses the selected research version.`)}</p>}
    {editing && <p className="flow-context-note warning">{t("有未保存的版本编辑。实验只使用已保存版本；请先保存或取消编辑，再提交。", "Version edits are unsaved. Experiments use saved versions only; save or cancel your edits before submitting.")}</p>}
    {run && activeStatuses.has(run.status) && <p className="fine-print">{t(`实验 ${shortId(run.id)} 正在处理，可以回看材料；结果不会因切换阶段而重新提交。`, `Experiment ${shortId(run.id)} is processing. You can inspect the source material; changing stages does not submit another experiment.`)}</p>}
    {stage === "report" && <p className="flow-context-note">{t("当前查看研究报告；返回结果审核可继续检查所选实验。", "You are viewing the research report. Return to Result review to inspect the selected experiment.")}</p>}
  </section>;
}
