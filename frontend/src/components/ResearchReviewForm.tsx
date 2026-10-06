import { useRef, useState, useEffect } from "react";
import type { FormEvent } from "react";
import { describeError, reviewPayload, statusName } from "../domain";
import type { Artifact, CandidateState, Run, Review } from "../domain";
import { sourceNames } from "../feedback";
import { assessmentDimensions, assessmentPayload, finishInterval } from "../researchAssessment";
import type { Dimension } from "../researchAssessment";
import { initialReviewDraft, recoveryKey, removeDurably } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { useReviewDraft } from "../useReviewDraft";
import { Field, JsonDetails } from "./ui";
import ReviewEvidence from "./ReviewEvidence";

type Perform = (label: string, action: () => Promise<void>) => Promise<void>;
export default function ResearchReviewForm({ workspaceId, run, allowNewReview, candidates, paperId, artifacts, busy, recording = false, onTimerRunningChange, onPerform, refresh }: {
  workspaceId: string; run: Run; allowNewReview: boolean; candidates: CandidateState[]; busy: string;
  paperId: string; artifacts: Artifact[];
  onPerform: Perform; refresh: () => Promise<void>; recording?: boolean; onTimerRunningChange?: (running: boolean) => void;
}) {
  const [candidate, setCandidate] = useState("");
  const [source, setSource] = useState("human");
  const [structured, setStructured] = useState(false);
  const [savedNotice, setSavedNotice] = useState("");
  const [completed, setCompleted] = useState(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const mutation = useRecoverableMutation<Review>(recoveryKey(workspaceId, "review-request", run.id), `/runs/${run.id}/reviews`);
  const target = run.review_targets?.find((value) => value.candidate_id === candidate);
  const draftKey = recoveryKey(workspaceId, "review-draft", run.id, candidate, target?.attempt_id || "", target?.result_digest || "");
  async function save(body?: Record<string, unknown>) {
    await onPerform(body ? "保存审核" : "安全重试审核", async () => {
      if (body && !allowNewReview) throw new Error("当前实验不能添加新审核；只能确认已经发送的原请求。");
      // The returned record contains the original frozen identity even when a
      // retried request is replayed after its run has advanced to another attempt.
      const saved = await mutation.execute(body);
      const savedKey = recoveryKey(workspaceId, "review-draft", saved.run_id, saved.candidate_id, saved.attempt_id, saved.result_digest);
      let localWarning = "";
      try {
        mutation.acknowledge(() => removeDurably(localStorage, savedKey));
        if (mounted.current) setCompleted((n) => n + 1);
      } catch (e) { localWarning = `本机恢复记录尚未清理；原请求已保留，请安全重试完成确认。${describeError(e)}`; }
      if (mounted.current) setSavedNotice(`审核已保存。${localWarning}`);
      try { await refresh(); }
      catch (e) { if (mounted.current) setSavedNotice(`审核已保存，列表刷新暂未成功；请刷新查看记录。${describeError(e)}${localWarning}`); }
    });
  }
  return <>
    {savedNotice && <p role="status">{savedNotice}</p>}
    {mutation.error && <p role="alert">{mutation.error}</p>}
    {mutation.pending && <section aria-label="审核提交恢复" className="info-note">
      <p>{mutation.pending.state === "rejected" ? "审核请求已明确拒绝，可以解除后修改。" : "审核提交结果待确认。请保留原请求并安全重试；不会生成新的请求标识。"}</p>
      <JsonDetails value={mutation.pending.body} label="查看冻结审核请求" />
      {mutation.pending.state === "rejected" ? <button className="button" disabled={!!busy} onClick={mutation.dismissRejected}>解除被拒绝的审核请求</button> :
        <button className="button" disabled={!!busy} onClick={() => void save()}>安全重试审核提交</button>}
    </section>}
    <Field label="审核候选"><select required disabled={!!busy || !!mutation.pending || !allowNewReview} value={candidate} onChange={(e) => { setCandidate(e.target.value); setSavedNotice(""); }}>
      <option value="">选择候选</option>{candidates.map((c) => <option key={c.id} value={c.id}>{c.id} · {statusName(c.status)}</option>)}
    </select></Field>
    <div className={candidate ? "review-evidence-grid" : undefined}>
    {candidate && <ReviewEvidence run={run} candidateId={candidate} paperId={paperId} artifacts={artifacts}/>}
    <div>{allowNewReview && candidate ? <ReviewEditor key={`${draftKey}:${completed}`} draftKey={draftKey} candidate={candidate} target={target}
      recording={recording} onTimerRunningChange={onTimerRunningChange} initialSource={source} initialStructured={structured} onSource={setSource} onStructured={setStructured}
      blocked={!!busy || !!mutation.pending || mutation.blocked || !mutation.ready || !target} onSave={save} /> :
      <p className="fine-print">{allowNewReview ? "请选择候选。审核草稿只在本机保存，按工作区、实验与结果版本隔离。" : "新审核暂不可用。原请求回放仅确认历史保存记录，不会重新审核当前实验。"}</p>}</div>
    </div>
  </>;
}

function ReviewEditor({ draftKey, candidate, target, initialSource, initialStructured, onSource, onStructured, blocked, onSave, recording, onTimerRunningChange }: {
  draftKey: string; candidate: string; target: { attempt_id: string; result_digest: string } | undefined;
  initialSource: string; initialStructured: boolean; onSource: (source: string) => void; onStructured: (structured: boolean) => void;
  blocked: boolean; onSave: (body: Record<string, unknown>) => Promise<void>; recording: boolean; onTimerRunningChange?: (running: boolean) => void;
}) {
  const initial = initialReviewDraft(initialSource, initialStructured);
  const store = useReviewDraft(draftKey, initial);
  const value = store.value;
  const draft = value.assessment;
  const [startedAt, setStartedAt] = useState<string | null>(null);
  const [timerError, setTimerError] = useState("");
  useEffect(() => { onTimerRunningChange?.(!!startedAt); return () => onTimerRunningChange?.(false); }, [startedAt, onTimerRunningChange]);
  const update = (next: Partial<typeof value>) => store.update({ ...value, ...next });
  async function submit(e: FormEvent) {
    e.preventDefault();
    try {
      if (!target) throw new Error("当前输出尚未获得可审核的版本标识，请刷新后重试。");
      const body = { expected_attempt_id: target.attempt_id, expected_result_digest: target.result_digest, ...reviewPayload(candidate, value.verdict, value.category, value.note, value.source),
        ...(value.structured ? { assessment: assessmentPayload(draft, target, startedAt) } : {}) };
      await onSave(body);
    } catch (e) { setTimerError(describeError(e)); }
  }
  function pause() {
    if (!startedAt) return;
    try { update({ assessment: finishInterval(draft, startedAt, new Date().toISOString()), timerWasRunning: false }); setStartedAt(null); setTimerError(""); }
    catch (e) { setTimerError(describeError(e)); }
  }
  return <form onSubmit={submit}>
    {store.error && <p role="alert">{store.error}</p>}
    {(store.offered || store.unreadable) && <section aria-label="审核草稿恢复" className="info-note">
      <p>{store.offered ? "发现此结果版本的本机审核草稿。请选择恢复或丢弃，不会自动应用。" : "此版本的草稿无法读取。请先另行保留浏览器记录，再明确丢弃损坏草稿。"}</p>
      {store.offered && <button type="button" className="button" disabled={blocked} onClick={() => { store.restore(); onSource(store.offered!.source); onStructured(store.offered!.structured); }}>恢复审核草稿</button>}
      <button type="button" className="button" disabled={blocked} onClick={store.clear}>丢弃审核草稿</button>
    </section>}
    {store.interrupted && <p role="status">上次主动计时已中断；未结束区间已排除，离开或停机期间不会计入工作时间。需要时请重新开始计时。</p>}
    <fieldset disabled={blocked || !store.ready || !!store.offered || store.unreadable}>
      <div className="form-columns">
        <Field label="审核结论"><select value={value.verdict} onChange={(e) => update({ verdict: e.target.value })}>
          <option value="accepted">接受</option><option value="needs_changes">需要修改</option><option value="rejected">拒绝</option>
        </select></Field>
        <Field label="审核维度"><select value={value.category} onChange={(e) => update({ category: e.target.value })}>
          {[["evidence", "论文证据"], ["hypothesis", "研究假设"], ["implementation", "候选实现"], ["data", "数据"], ["evaluation", "评估"], ["other", "其他"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select></Field>
      </div>
      <Field label="审核依据"><textarea required rows={4} value={value.note} onChange={(e) => update({ note: e.target.value })} placeholder="引用了哪段证据？哪里符合预期，或需要修正？" /></Field>
      <Field label="审核声明来源" hint="来源和审核人均为声明，不等于认证身份；自动测试请选择自动验收样例。">
        <select value={value.source} onChange={(e) => { update({ source: e.target.value }); onSource(e.target.value); }}>{Object.entries(sourceNames).map(([v, label]) => <option key={v} value={v}>{label}</option>)}</select>
      </Field>
      <label><input type="checkbox" checked={value.structured} disabled={!!startedAt} onChange={(e) => { update({ structured: e.target.checked }); onStructured(e.target.checked); }} /> 添加分项研究审核</label>
      <p className="fine-print">通用“接受”不代表研究语义已通过。草稿按冻结结果保存在本机；离开后需显式恢复，未结束计时不会续算。</p>
      {value.structured && <fieldset>
        <legend>分项研究审核</legend>
        <Field label="审核人标识"><input required maxLength={120} value={draft.reviewer} onChange={(e) => update({ assessment: { ...draft, reviewer: e.target.value } })} /></Field>
        {Object.entries(assessmentDimensions).map(([key, label]) => <div key={key}>
          <Field label={`${label}判断`}><select value={draft.dimensions[key as Dimension].outcome} onChange={(e) => update({ assessment: { ...draft, dimensions: { ...draft.dimensions, [key]: { ...draft.dimensions[key as Dimension], outcome: e.target.value } } } })}>
            <option value="not_assessed">未评定</option><option value="passed">通过</option><option value="failed">未通过</option><option value="not_applicable">不适用</option>
          </select></Field>
          <Field label={`${label}理由`}><textarea required maxLength={2000} rows={2} value={draft.dimensions[key as Dimension].reason} onChange={(e) => update({ assessment: { ...draft, dimensions: { ...draft.dimensions, [key]: { ...draft.dimensions[key as Dimension], reason: e.target.value } } } })} /></Field>
        </div>)}
        <p className="fine-print">“不适用”和“未评定”都不会成为完整通过；原文没有解释经济机制时，应核对是否明确标注了自己的推测。</p>
        {recording && <p className="info-note">正在记录流程观测；本次审核不再启动独立计时。此前已结束的审核区间仍保留。{startedAt && "请先暂停原审核计时，避免重叠。"}</p>}
        {(!recording || startedAt || draft.active_intervals.length > 0) && <details open={recording || undefined}>
        <summary>可选：本次审核的主动工作时间</summary>
        <p>已记录 {draft.active_intervals.length} 个区间{startedAt ? "；正在计时，请在等待或离开前暂停。" : "；未记录区间表示未测量。"}</p>
        <div className="button-row">
          {!recording && <button type="button" className="button small" disabled={!!startedAt || draft.active_intervals.length >= 100} onClick={() => { update({ timerWasRunning: true }); setStartedAt(new Date().toISOString()); setTimerError(""); }}>开始主动工作计时</button>}
          <button type="button" className="button small" disabled={!startedAt} onClick={pause}>暂停主动工作计时</button>
        </div>
        {draft.active_intervals.map((interval, index) => <p className="fine-print" key={index}>{interval.started_at} → {interval.ended_at}</p>)}
        <p className="fine-print">仅保存显式区间；这不是完整研究耗时或节省时间的证明，后台等待不会自动扣除。保存前必须暂停。</p>
        </details>}
      </fieldset>}
      {timerError && <p role="alert">{timerError}</p>}
      <button className="button primary" disabled={!value.note.trim() || !!startedAt} type="submit">保存审核</button>
    </fieldset>
  </form>;
}
