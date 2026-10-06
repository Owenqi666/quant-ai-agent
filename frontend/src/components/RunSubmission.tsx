import { useEffect, useRef, useState } from "react";
import { describeError, modes } from "../domain";
import type { Revision, Run } from "../domain";
import { recoveryKey } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { Field, JsonDetails } from "./ui";

/** One pending operation per research, independent of the currently viewed revision. */
export default function RunSubmission({workspaceId, researchId, revision, workerOnline, busy, onQueued}: {
  workspaceId: string; researchId: string; revision: Revision; workerOnline: boolean; busy: string;
  onQueued: (run: Run) => Promise<void>;
}) {
  const [mode, setMode] = useState("normalized_fixed");
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const mutation = useRecoverableMutation<Run>(recoveryKey(workspaceId,"run-request",researchId), "/runs");
  async function submit(body?: Record<string, unknown>) {
    setSending(true); setError("");
    try {
      const result = await mutation.execute(body);
      try { mutation.acknowledge(); }
      catch (e) {
        if (mounted.current) setNotice(`实验已保存到队列；本机请求尚未清理，请用原请求安全重试。${describeError(e)}`);
        return;
      }
      if (mounted.current) {
        setNotice("实验已保存到队列。");
        try { await onQueued(result); }
        catch (e) { if (mounted.current) setNotice(`实验已保存到队列，列表刷新暂未成功。${describeError(e)}`); }
      }
    } catch (e) { if (mounted.current) setError(describeError(e)); }
    finally { if (mounted.current) setSending(false); }
  }
  return <section className="panel launch-panel">
    <div className="panel-heading"><h2>提交实验</h2></div>
    {notice && <p role="status">{notice}</p>}
    {(mutation.error || error) && <p role="alert">{mutation.error || error}</p>}
    {mutation.pending && <section aria-label="实验提交恢复" className="info-note">
      <p>{mutation.pending.state === "rejected" ? "实验请求已明确拒绝，可以解除后修改。" : "实验提交结果待确认。请安全重试原请求；请求中的研究版本和执行方式保持冻结。"}</p>
      <JsonDetails value={mutation.pending.body} label="查看冻结实验请求" />
      {mutation.pending.state === "rejected" ? <button className="button" disabled={sending || !!busy} onClick={mutation.dismissRejected}>解除被拒绝的实验请求</button> :
        <button className="button" disabled={sending || !!busy} onClick={() => void submit()}>安全重试实验提交</button>}
    </section>}
    <p className="muted">执行 v{revision.number}，保存输入快照与独立结果。</p>
    <fieldset disabled={sending || !!busy || !!mutation.pending || !mutation.ready || mutation.blocked}>
      <Field label="执行方式"><select value={mode} onChange={(e) => setMode(e.target.value)}>
        {modes.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
      </select></Field>
      <p className="mode-description">{modes.find((m) => m.value === mode)?.description}</p>
      <dl className="budget-list">
        <div><dt>运行预算</dt><dd>{String(revision.task.budget.max_seconds)} 秒</dd></div>
        <div><dt>工具调用上限</dt><dd>{String(revision.task.budget.max_tool_calls)} 次</dd></div>
        <div><dt>每个候选尝试上限</dt><dd>{String(revision.task.budget.max_attempts_per_candidate)} 次</dd></div>
      </dl>
      <button className="button primary wide" disabled={!revision.task.candidates.length} onClick={() => void submit({revision_id:revision.id, mode})}>
        {sending ? "提交中…" : "提交实验"}
      </button>
    </fieldset>
    {!workerOnline && <p className="inline-warning">Worker 当前离线，任务会保留在队列中等待执行。</p>}
    <p className="fine-print">完成状态表示流程结束，个别候选仍可能受阻或无法评估。刷新后可回到此研究确认原提交。</p>
  </section>;
}
