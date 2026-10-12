import { useEffect, useRef, useState } from "react";
import { describeError, modes } from "../domain";
import type { Dataset, Revision, Run } from "../domain";
import { recoveryKey } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { useI18n } from "../i18n";
import { Field, JsonDetails } from "./ui";
import { TaskConfigurationSummary } from "./TaskEditor";
import "./experimentFlow.css";

/** One pending operation per research, independent of the currently viewed revision. */
export default function RunSubmission({workspaceId, researchId, revision, dataset, workerOnline, busy, onQueued, onSubmitting, collapsed = false}: {
  workspaceId: string; researchId: string; revision: Revision; dataset?: Dataset; workerOnline: boolean; busy: string;
  onQueued: (run: Run) => Promise<void>; onSubmitting?: () => void; collapsed?: boolean;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState("normalized_fixed");
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const mutation = useRecoverableMutation<Run>(recoveryKey(workspaceId,"run-request",researchId), "/runs");
  const englishModes: Record<string, {label: string; description: string}> = {
    normalized_fixed: { label: "Normalized fixed workflow", description: "Normalize registered operator aliases, then execute once. Recommended baseline." },
    fixed: { label: "Original fixed workflow", description: "Execute the original expression directly. Failures are retained without automatic repairs." },
    agent: { label: "Bounded rule-based repair", description: "Repair known alias errors within the budget. No AI model is called." },
  };
  const selectedMode = modes.find((entry) => entry.value === mode)!;
  async function submit(body?: Record<string, unknown>) {
    setSending(true); setError(""); setNotice("");
    onSubmitting?.();
    try {
      const result = await mutation.execute(body);
      try { mutation.acknowledge(); }
      catch (e) {
        if (mounted.current) setNotice(t("实验已保存到队列；本机请求尚未清理，请用原请求安全重试。", "The experiment was queued. The local request could not be cleared; safely retry the original request.") + describeError(e));
        return;
      }
      if (mounted.current) {
        setNotice(t("实验已保存到队列。", "Experiment saved to the queue."));
        try { await onQueued(result); }
        catch (e) { if (mounted.current) setNotice(t("实验已保存到队列，列表刷新暂未成功。", "The experiment was queued, but refreshing the list failed.") + describeError(e)); }
      }
    } catch (e) { if (mounted.current) setError(describeError(e)); }
    finally { if (mounted.current) setSending(false); }
  }
  return <details id="daily-run-submission" open={!collapsed || !!mutation.pending || mutation.blocked || !!mutation.error || !!error || sending}>
    <summary>{t("配置并提交新实验", "Configure and submit a new experiment")}</summary>
    <section className="panel launch-panel experiment-flow" aria-label={t("实验配置与提交", "Experiment configuration and submission")}>
    <div className="panel-heading"><div><h2>{t("提交实验", "Submit experiment")}</h2><p className="muted">{t(`执行 v${revision.number}，保存输入快照与独立结果。`, `Execute v${revision.number} and save its input snapshot and independent results.`)}</p></div><span className="flow-version">v{revision.number}</span></div>
    {notice && <p className="info-note" role="status">{notice}</p>}
    {(mutation.error || error) && <p className="info-note warning" role="alert">{mutation.error || error}</p>}
    {mutation.pending && <section aria-label={t("实验提交恢复", "Experiment submission recovery")} className="info-note flow-recovery">
      <p>{mutation.pending.state === "rejected" ? t("实验请求已明确拒绝，可以解除后修改。", "The experiment request was explicitly rejected. Release it before editing.") : t("实验提交结果待确认。请安全重试原请求；请求中的研究版本和执行方式保持冻结。", "The submission outcome is uncertain. Safely retry the original request; its research revision and execution mode remain frozen.")}</p>
      <dl className="flow-summary"><div><dt>{t("冻结版本", "Frozen revision")}</dt><dd><code>{String(mutation.pending.body.revision_id ?? "—")}</code></dd></div><div><dt>{t("冻结执行方式", "Frozen execution mode")}</dt><dd><code>{String(mutation.pending.body.mode ?? "—")}</code></dd></div></dl>
      <JsonDetails value={mutation.pending.body} label={t("查看冻结实验请求", "View frozen experiment request")} />
      {mutation.pending.state === "rejected" ? <button type="button" className="button" disabled={sending || !!busy} onClick={mutation.dismissRejected}>{t("解除被拒绝的实验请求", "Release rejected experiment request")}</button> :
        <button type="button" className="button" disabled={sending || !!busy} onClick={() => void submit()}>{t("安全重试实验提交", "Safely retry experiment submission")}</button>}
    </section>}
    <TaskConfigurationSummary task={revision.task} dataset={dataset} />
    <fieldset className="flow-launch-controls" disabled={sending || !!busy || !!mutation.pending || !mutation.ready || mutation.blocked}>
      <legend>{t("选择执行方式", "Choose execution mode")}</legend>
      <Field label={t("执行方式", "Execution mode")}><select value={mode} onChange={(e) => setMode(e.target.value)}>
        {modes.map((entry) => <option key={entry.value} value={entry.value}>{t(entry.label, englishModes[entry.value].label)}</option>)}
      </select></Field>
      <p className="mode-description">{t(selectedMode.description, englishModes[mode].description)}</p>
      <dl className="budget-list flow-budget-summary">
        <div><dt>{t("运行预算", "Runtime budget")}</dt><dd>{String(revision.task.budget.max_seconds)} {t("秒", "seconds")}</dd></div>
        <div><dt>{t("工具调用上限", "Tool-call limit")}</dt><dd>{String(revision.task.budget.max_tool_calls)} {t("次", "calls")}</dd></div>
        <div><dt>{t("每个候选尝试上限", "Attempts per candidate")}</dt><dd>{String(revision.task.budget.max_attempts_per_candidate)}</dd></div>
        <div><dt>{t("最多候选数", "Candidate limit")}</dt><dd>{String(revision.task.budget.max_candidates)}</dd></div>
      </dl>
      <div className="flow-decision">
        <p>{t(`提交将执行已保存 v${revision.number} 的完整候选集合。研究定义的修改需先保存新版本。`, `Submission executes the complete candidate set saved in v${revision.number}. Save changes as a new revision before submitting them.`)}</p>
        <button type="button" className="button primary" disabled={!revision.task.candidates.length} onClick={() => void submit({revision_id:revision.id, mode})}>
          {sending ? t("提交中…", "Submitting…") : t("提交实验", "Submit experiment")}
        </button>
      </div>
    </fieldset>
    {!workerOnline && <p className="inline-warning" role="status">{t("Worker 当前离线，任务会保留在队列中等待执行。", "The worker is offline. The task will remain queued until it can run.")}</p>}
    {dataset?.data_kind === "synthetic" && <p className="info-note warning">{t("合成数据仅用于软件与流程验证，不能据此判断投资表现。", "Synthetic data supports software and workflow validation only; it cannot establish investment performance.")}</p>}
    <p className="fine-print">{t("完成状态表示流程结束，个别候选仍可能受阻或无法评估。刷新后可回到此研究确认原提交。", "Completed means the workflow has ended; individual candidates may still be blocked or not evaluable. After refreshing, return to this research to confirm the original submission.")}</p>
    <JsonDetails value={revision.task} label={t("查看完整已保存任务", "View the complete saved task")} />
  </section></details>;
}
