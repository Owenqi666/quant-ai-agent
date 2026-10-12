import { useRef, useState, useEffect, useCallback } from "react";
import type { FormEvent } from "react";
import { describeError, reviewPayload, shortId, statusName } from "../domain";
import type { Artifact, CandidateState, Run, Review } from "../domain";
import { sourceNames } from "../feedback";
import { assessmentDimensions, assessmentPayload, finishInterval } from "../researchAssessment";
import type { Dimension } from "../researchAssessment";
import { initialReviewDraft, recoveryKey, removeDurably, requireReviewVerdict } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { useReviewDraft } from "../useReviewDraft";
import { observedMetric, reviewEvidence } from "../reviewEvidence";
import { useI18n } from "../i18n";
import { Badge, Field, JsonDetails } from "./ui";
import ReviewEvidence from "./ReviewEvidence";
import "./resultReview.css";

type Perform = (label: string, action: () => Promise<void>) => Promise<void>;
export function CandidateComparison({ run, candidates, artifacts, selected, disabled, onSelect }: {
  run: Run; candidates: CandidateState[]; artifacts: Artifact[]; selected: string;
  disabled: boolean; onSelect: (id: string, trigger: HTMLElement) => void;
}) {
  const { t } = useI18n();
  const origins: Record<string, string> = { paper_original: t("论文原文", "Paper original"), user_modification: t("用户/项目修改", "User/project modification"), model_conjecture: t("模型推测", "Model conjecture") };
  const metric = (value: unknown, percent = false) => typeof value === "number" && Number.isFinite(value) ? observedMetric(value, percent) : t("未计算", "Not calculated");
  return <section className="result-comparison" aria-label={t("候选输出对照", "Compare candidate outputs")}>
    <div className="result-section-heading"><h3>{t("候选输出对照", "Compare candidate outputs")}</h3><span className="fine-print">{t("冻结版本 ", "Frozen revision ")}{shortId(run.revision_id)}</span></div>
    <p className="fine-print">{t("选择候选查看已核验的输出。未执行、阻断和缺失结果保留原状态，不用零替代。", "Select a candidate to inspect verified output. Unexecuted, blocked and missing results retain their status; missing values are not replaced with zero.")}</p>
    <div className="table-scroll"><table aria-label={t("候选输出比较", "Candidate output comparison")}><thead><tr>
      <th>{t("候选", "Candidate")}</th><th>{t("声明来源链", "Declared source chain")}</th><th>{t("执行结果", "Execution result")}</th><th>{t("日均毛收益", "Mean daily gross return")}</th><th>Mean Rank IC</th><th>{t("评估日数", "Evaluated days")}</th><th>{t("覆盖率", "Coverage")}</th>
    </tr></thead><tbody>{candidates.map(candidate => {
      const output = reviewEvidence(run, candidate.id, artifacts), metrics = output?.candidate.result?.metrics;
      return <tr key={candidate.id} className={selected === candidate.id ? "result-selected-row" : undefined}>
        <th scope="row"><button type="button" className="result-candidate-select" aria-pressed={selected === candidate.id} disabled={disabled}
          onClick={event => onSelect(candidate.id, event.currentTarget)}>{candidate.id}</button></th>
        <td className="result-source-cell">{output?.declared ? (origins[output.declared.origin] || String(output.declared.origin) + t("（未识别声明）", " (unrecognized declaration)")) : t("声明未知", "Declaration unknown")}<span className="cell-subtitle">{t("假说：", "Hypothesis: ")}{output?.hypothesis?.id || t("关联未知", "Link unknown")}<br/>{t("证据：", "Evidence: ")}{output?.evidenceReferences.map(reference => reference.id + (!reference.evidence ? t("（未知）", " (unknown)") : "")).join(" · ") || t("未记录", "Not recorded")}</span>{!!output?.sourceIssues.length && <span className="cell-subtitle">{t("来源有未确认项", "Source has unconfirmed items")}</span>}</td>
        <td><Badge status={candidate.status}/>{candidate.reason && <span className="cell-subtitle">{candidate.reason}</span>}</td>
        <td>{metric(metrics?.mean_gross_return, true)}</td><td>{metric(metrics?.mean_rank_ic)}</td><td>{metrics?.evaluated_days ?? t("未计算", "Not calculated")}</td><td>{metric(metrics?.factor_coverage, true)}</td>
      </tr>;
    })}</tbody></table></div>
  </section>;
}
export default function ResearchReviewForm({ workspaceId, run, allowNewReview, candidates, artifacts, busy, recording = false, onTimerRunningChange, onReviewDirtyChange, onPerform, refresh }: {
  workspaceId: string; run: Run; allowNewReview: boolean; candidates: CandidateState[]; busy: string;
  paperId: string; artifacts: Artifact[];
  onPerform: Perform; refresh: () => Promise<void>; recording?: boolean;
  onTimerRunningChange?: (running: boolean) => void; onReviewDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useI18n();
  const [candidate, setCandidate] = useState(candidates[0]?.id || "");
  const [source, setSource] = useState("human");
  const [structured, setStructured] = useState(false);
  const [savedNotice, setSavedNotice] = useState("");
  const [completed, setCompleted] = useState(0);
  const [timerRunning, setTimerRunning] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [pendingCandidate, setPendingCandidate] = useState("");
  const leaveDialog = useRef<HTMLDialogElement>(null);
  const continueEditing = useRef<HTMLButtonElement>(null);
  const candidateTrigger = useRef<HTMLElement | null>(null);
  const reportTimerRunning = useCallback((running: boolean) => {
    setTimerRunning(running); onTimerRunningChange?.(running);
  }, [onTimerRunningChange]);
  const reportDirty = useCallback((value: boolean) => { setDirty(value); onReviewDirtyChange?.(value); }, [onReviewDirtyChange]);
  useEffect(() => () => onReviewDirtyChange?.(false), [onReviewDirtyChange]);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (!candidates.some(item => item.id === candidate)) setCandidate(candidates[0]?.id || "");
  }, [candidate, candidates]);
  useEffect(() => {
    if (pendingCandidate) { leaveDialog.current?.showModal(); continueEditing.current?.focus(); }
  }, [pendingCandidate]);
  const mutation = useRecoverableMutation<Review>(recoveryKey(workspaceId, "review-request", run.id), "/runs/" + run.id + "/reviews");
  const target = reviewEvidence(run, candidate, artifacts)?.target;
  const draftKey = recoveryKey(workspaceId, "review-draft", run.id, candidate, target?.attempt_id || "", target?.result_digest || "");
  const selectionBlocked = !!busy || !!mutation.pending || !allowNewReview || timerRunning;
  function selectCandidate(id: string, trigger: HTMLElement) {
    if (selectionBlocked || id === candidate) return;
    if (dirty) { candidateTrigger.current = trigger; setPendingCandidate(id); return; }
    setCandidate(id); setSavedNotice("");
  }
  function closeCandidateDialog(leave: boolean) {
    const next = pendingCandidate;
    leaveDialog.current?.close(); setPendingCandidate("");
    if (leave) { setCandidate(next); setSavedNotice(""); }
    candidateTrigger.current?.focus();
  }
  async function save(body?: Record<string, unknown>) {
    await onPerform(body ? t("保存审核", "Save review") : t("安全重试审核", "Safely retry review"), async () => {
      if (body && !allowNewReview) throw new Error(t("当前实验不能添加新审核；只能确认已经发送的原请求。", "This run cannot receive a new review; only the original submitted request can be confirmed."));
      const saved = await mutation.execute(body);
      const savedKey = recoveryKey(workspaceId, "review-draft", saved.run_id, saved.candidate_id, saved.attempt_id, saved.result_digest);
      let localWarning = "";
      try {
        mutation.acknowledge(() => removeDurably(localStorage, savedKey));
        if (mounted.current) { setCompleted(n => n + 1); reportDirty(false); }
      } catch (e) { localWarning = t("本机恢复记录尚未清理；原请求已保留，请安全重试完成确认。", "The local recovery record could not be cleared. The original request is retained; safely retry to finish confirmation. ") + describeError(e); }
      if (mounted.current) setSavedNotice(t("审核已保存。", "Review saved. ") + localWarning);
      try { await refresh(); }
      catch (e) { if (mounted.current) setSavedNotice(t("审核已保存，列表刷新暂未成功；请刷新查看记录。", "The review was saved, but the list could not refresh. Refresh to see the record. ") + describeError(e) + localWarning); }
    });
  }
  return <>
    {savedNotice && <p role="status">{savedNotice}</p>}
    {mutation.error && <p role="alert">{mutation.error}</p>}
    {mutation.pending && <section aria-label={t("审核提交恢复", "Recover review submission")} className="info-note">
      <p>{mutation.pending.state === "rejected" ? t("审核请求已明确拒绝，可以解除后修改。", "The review request was explicitly rejected. Release it before editing.") : t("审核提交结果待确认。请保留原请求并安全重试；不会生成新的请求标识。", "The review submission is unconfirmed. Keep and safely retry the original request; no new request identity will be generated.")}</p>
      <JsonDetails value={mutation.pending.body} label={t("查看冻结审核请求", "View the frozen review request")}/>
      {mutation.pending.state === "rejected" ? <button type="button" className="button" disabled={!!busy} onClick={mutation.dismissRejected}>{t("解除被拒绝的审核请求", "Release rejected review request")}</button> :
        <button type="button" className="button" disabled={!!busy} onClick={() => void save()}>{t("安全重试审核提交", "Safely retry review submission")}</button>}
    </section>}
    {candidates.length > 0 && <CandidateComparison run={run} candidates={candidates} artifacts={artifacts} selected={candidate} disabled={selectionBlocked} onSelect={selectCandidate}/>}
    <Field label={t("审核候选", "Review candidate")}><select required disabled={selectionBlocked} value={candidate} onChange={e => selectCandidate(e.target.value, e.currentTarget)}>
      <option value="">{t("选择候选", "Choose a candidate")}</option>{candidates.map(c => <option key={c.id} value={c.id}>{c.id} · {statusName(c.status)}</option>)}
    </select></Field>
    {timerRunning && <p className="fine-print">{t("切换候选前请先暂停本次审核计时；已结束的区间会保留。", "Pause active review timing before switching candidates. Completed intervals are retained.")}</p>}
    <div className={candidate ? "review-evidence-stack" : undefined}>
      {candidate && <ReviewEvidence run={run} candidateId={candidate} artifacts={artifacts}/>}
      <div>{allowNewReview && candidate ? <ReviewEditor key={draftKey + ":" + completed} draftKey={draftKey} candidate={candidate} target={target}
        recording={recording} onTimerRunningChange={reportTimerRunning} onDirtyChange={reportDirty}
        initialSource={source} initialStructured={structured} onSource={setSource} onStructured={setStructured}
        blocked={!!busy || !!mutation.pending || mutation.blocked || !mutation.ready || !target} onSave={save}/> :
        <p className="fine-print">{allowNewReview ? t("请选择候选。审核草稿只在本机保存，按工作区、实验与结果版本隔离。", "Choose a candidate. Review drafts are local and scoped to the workspace, run and result identity.") : t("新审核暂不可用。原请求回放仅确认历史保存记录，不会重新审核当前实验。", "New reviews are unavailable. Replaying an original request confirms its historical save; it does not review the current run again.")}</p>}</div>
    </div>
    <dialog ref={leaveDialog} className="result-leave-dialog" aria-labelledby="review-candidate-leave-title" onCancel={event => { event.preventDefault(); closeCandidateDialog(false); }}>
      <h3 id="review-candidate-leave-title">{t("切换前保留这份审核草稿？", "Keep this review draft before switching?")}</h3>
      <p>{t("当前意见尚未保存到服务端。本机草稿仍按当前候选和冻结结果保留，返回后需明确恢复。若浏览器提示保存失败，请先复制内容。", "This review has not been saved to the server. Its local draft remains scoped to this candidate and frozen result; restoring it later is explicit. If local storage failed, copy the text before leaving.")}</p>
      <div className="button-row"><button ref={continueEditing} type="button" onClick={() => closeCandidateDialog(false)}>{t("继续编辑", "Keep editing")}</button><button type="button" onClick={() => closeCandidateDialog(true)}>{t("保留草稿并切换", "Keep draft and switch")}</button></div>
    </dialog>
  </>;
}
function ReviewEditor({ draftKey, candidate, target, initialSource, initialStructured, onSource, onStructured, blocked, onSave, recording, onTimerRunningChange, onDirtyChange }: {
  draftKey: string; candidate: string; target: { attempt_id: string; result_digest: string } | undefined;
  initialSource: string; initialStructured: boolean; onSource: (source: string) => void; onStructured: (structured: boolean) => void;
  blocked: boolean; onSave: (body: Record<string, unknown>) => Promise<void>; recording: boolean;
  onTimerRunningChange?: (running: boolean) => void; onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useI18n();
  const initial = initialReviewDraft(initialSource, initialStructured);
  const baseline = useRef(JSON.stringify(initial));
  const store = useReviewDraft(draftKey, initial), value = store.value, draft = value.assessment;
  const [startedAt, setStartedAt] = useState<string | null>(null);
  const [timerError, setTimerError] = useState("");
  const changed = JSON.stringify(value) !== baseline.current;
  useEffect(() => { onDirtyChange?.(changed); return () => onDirtyChange?.(false); }, [changed, onDirtyChange]);
  useEffect(() => { onTimerRunningChange?.(!!startedAt); return () => onTimerRunningChange?.(false); }, [startedAt, onTimerRunningChange]);
  const update = (next: Partial<typeof value>) => store.update({ ...value, ...next });
  const dimensions: Record<Dimension, string> = {
    evidence_accuracy: t("引用与页码准确", "Quotation and page accuracy"),
    hypothesis_fidelity: t("假设忠于所引原文", "Fidelity to the cited source"),
    mechanism_attribution: t("经济解释归因恰当", "Economic explanation attribution"),
    field_semantics: t("字段含义与替代说明", "Field semantics and substitutions"),
    implementation_alignment: t("实现与声明公式一致", "Implementation and formula alignment"),
  };
  const sources: Record<string, string> = { human: t("人工审核", "Human review"), automation: t("自动验收样例", "Automated acceptance example"), imported: t("导入记录", "Imported record"), legacy_unknown: t("历史来源未记录", "Legacy source not recorded") };
  async function submit(e: FormEvent) {
    e.preventDefault();
    try {
      if (!target) throw new Error(t("当前输出尚未获得可审核的版本标识，请刷新后重试。", "This output has no reviewable identity. Refresh and try again."));
      if (!value.category) throw new Error(t("请选择本次审核维度。", "Choose the dimension to review."));
      const body = { expected_attempt_id: target.attempt_id, expected_result_digest: target.result_digest, ...reviewPayload(candidate, requireReviewVerdict(value.verdict), value.category, value.note, value.source),
        ...(value.structured ? { assessment: assessmentPayload(draft, target, startedAt) } : {}) };
      await onSave(body);
    } catch (e) { setTimerError(describeError(e)); }
  }
  function pause() {
    if (!startedAt) return;
    try { update({ assessment: finishInterval(draft, startedAt, new Date().toISOString()), timerWasRunning: false }); setStartedAt(null); setTimerError(""); }
    catch (e) { setTimerError(describeError(e)); }
  }
  return <form onSubmit={submit} className="review-judgment" aria-label={t("记录本次审核判断", "Record this review")}>
    <h3>{t("记录独立判断", "Record an independent judgment")}</h3>
    <dl className="result-judgment-dimensions">
      <div><dt>{t("工程执行", "Engineering execution")}</dt><dd>{t("根据上方执行状态、实际指标与冻结产物判断。", "Assess execution status, computed metrics and frozen artifacts above.")}</dd></div>
      <div><dt>{t("论文忠实度", "Paper fidelity")}</dt><dd>{t("仍需对照原文、公式与归因，文件完整性不替代这一判断。", "Compare the source, formula and attribution. File integrity does not establish fidelity.")}</dd></div>
      <div><dt>{t("投资表现", "Investment performance")}</dt><dd>{t("合成数据不能证明投资有效性；需要真实市场和成本检验。", "Synthetic data cannot establish investment validity. Real-market data and cost validation are required.")}</dd></div>
    </dl>
    {target && <p className="review-binding">{t("本次审核绑定 ", "Review target: ")}<strong>{candidate}</strong> · {t("尝试 ", "Attempt ")}{shortId(target.attempt_id)} · {t("结果摘要 ", "Result digest ")}{shortId(target.result_digest)}<br/>{t("草稿仅存于本机。保存审核后，服务端会保留针对该冻结结果的记录。", "Drafts remain local. Saving creates a server record tied to this frozen result.")}</p>}
    {store.error && <p role="alert">{store.error}</p>}
    {(store.offered || store.unreadable) && <section aria-label={t("审核草稿恢复", "Restore review draft")} className="info-note">
      <p>{store.offered ? t("发现此结果版本的本机审核草稿。请选择恢复或丢弃，不会自动应用。", "A local review draft exists for this result. Choose to restore or discard it; it is not applied automatically.") : t("此版本的草稿无法读取。请先另行保留浏览器记录，再明确丢弃损坏草稿。", "This result's draft cannot be read. Preserve the browser record elsewhere before explicitly discarding the damaged draft.")}</p>
      {store.offered && <button type="button" className="button" disabled={blocked} onClick={() => { store.restore(); onSource(store.offered!.source); onStructured(store.offered!.structured); }}>{t("恢复审核草稿", "Restore review draft")}</button>}
      <button type="button" className="button" disabled={blocked} onClick={store.clear}>{t("丢弃审核草稿", "Discard review draft")}</button>
    </section>}
    {store.interrupted && <p role="status">{t("上次主动计时已中断；未结束区间已排除，离开或停机期间不会计入工作时间。需要时请重新开始计时。", "The previous timer was interrupted. Unfinished intervals were excluded; time away or offline is not counted. Start a new interval when needed.")}</p>}
    <fieldset disabled={blocked || !store.ready || !!store.offered || store.unreadable}>
      <legend>{t("记录判断与理由", "Verdict and rationale")}</legend>
      <p className="fine-print">{t("先核对上方冻结依据与实际输出，再选择本次审核维度和结论。结论只表示所选维度的判断，不会自动停止候选或证明研究有效。", "Inspect the frozen evidence and output before choosing a dimension and verdict. The verdict applies only to that dimension; it does not stop candidates or prove research validity.")}</p>
      <div className="form-columns">
        <Field label={t("审核结论", "Review verdict")}><select required value={value.verdict} onChange={e => update({ verdict: e.target.value })}>
          <option value="">{t("请选择结论", "Choose a verdict")}</option><option value="accepted">{t("接受", "Accept")}</option><option value="needs_changes">{t("需要修改", "Needs changes")}</option><option value="rejected">{t("拒绝", "Reject")}</option>
        </select></Field>
        <Field label={t("审核维度", "Review dimension")}><select required value={value.category} onChange={e => update({ category: e.target.value })}>
          {[["evidence", t("论文证据", "Paper evidence")], ["hypothesis", t("研究假设", "Research hypothesis")], ["implementation", t("候选实现", "Candidate implementation")], ["data", t("数据", "Data")], ["evaluation", t("评估", "Evaluation")], ["other", t("其他", "Other")]].map(([v, label]) => <option key={v} value={v}>{label}</option>)}
        </select></Field>
      </div>
      <Field label={t("审核依据", "Review rationale")}><textarea required rows={4} value={value.note} onChange={e => update({ note: e.target.value })} placeholder={t("引用了哪段证据？哪里符合预期，或需要修正？", "Which evidence supports the judgment? What matches expectations, or needs correction?")}/></Field>
      <details className="review-recording-options" open={value.structured || undefined}>
        <summary>{t("可选：分项审核与记录设置", "Optional: detailed assessment and recording")}</summary>
        <Field label={t("审核声明来源", "Declared review source")} hint={t("来源和审核人均为声明，不等于认证身份；自动测试请选择自动验收样例。", "Source and reviewer are declarations, not authenticated identities. Select automated acceptance example for automated tests.")}>
          <select value={value.source} onChange={e => { update({ source: e.target.value }); onSource(e.target.value); }}>{Object.keys(sourceNames).map(v => <option key={v} value={v}>{sources[v] || v}</option>)}</select>
        </Field>
        <label><input type="checkbox" checked={value.structured} disabled={!!startedAt} onChange={e => { update({ structured: e.target.checked }); onStructured(e.target.checked); }}/> {t("添加分项研究审核", "Add a detailed research assessment")}</label>
        <p className="fine-print">{t("通用“接受”不代表研究语义已通过。草稿按冻结结果保存在本机；离开后需显式恢复，未结束计时不会续算。", "A general accept verdict does not establish semantic validity. Drafts remain local and scoped to the frozen result. Restoration is explicit and unfinished timing does not continue.")}</p>
        {value.structured && <fieldset><legend>{t("分项研究审核", "Detailed research assessment")}</legend>
          <Field label={t("审核人标识", "Reviewer identifier")}><input required maxLength={120} value={draft.reviewer} onChange={e => update({ assessment: { ...draft, reviewer: e.target.value } })}/></Field>
          {Object.keys(assessmentDimensions).map(name => { const key = name as Dimension; return <div key={key}>
            <Field label={dimensions[key] + t("判断", " verdict")}><select value={draft.dimensions[key].outcome} onChange={e => update({ assessment: { ...draft, dimensions: { ...draft.dimensions, [key]: { ...draft.dimensions[key], outcome: e.target.value } } } })}>
              <option value="not_assessed">{t("未评定", "Not assessed")}</option><option value="passed">{t("通过", "Passed")}</option><option value="failed">{t("未通过", "Failed")}</option><option value="not_applicable">{t("不适用", "Not applicable")}</option>
            </select></Field>
            <Field label={dimensions[key] + t("理由", " rationale")}><textarea required maxLength={2000} rows={2} value={draft.dimensions[key].reason} onChange={e => update({ assessment: { ...draft, dimensions: { ...draft.dimensions, [key]: { ...draft.dimensions[key], reason: e.target.value } } } })}/></Field>
          </div>; })}
          <p className="fine-print">{t("“不适用”和“未评定”都不会成为完整通过；原文没有解释经济机制时，应核对是否明确标注了自己的推测。", "Not applicable and not assessed do not count as a complete pass. When the source omits an economic mechanism, check that any conjecture is attributed explicitly.")}</p>
          {recording && <p className="info-note">{t("正在记录流程观测；本次审核不再启动独立计时。此前已结束的审核区间仍保留。", "A workflow observation is being recorded. Do not start a separate review timer; completed review intervals are retained.")}{startedAt && t("请先暂停原审核计时，避免重叠。", " Pause the existing review timer to avoid overlap.")}</p>}
          {(!recording || startedAt || draft.active_intervals.length > 0) && <details open={recording || undefined}>
            <summary>{t("可选：本次审核的主动工作时间", "Optional: active review time")}</summary>
            <p>{t("已记录 ", "Recorded ")}{draft.active_intervals.length}{t(" 个区间", " intervals")}{startedAt ? t("；正在计时，请在等待或离开前暂停。", "; timing is active. Pause before waiting or leaving.") : t("；未记录区间表示未测量。", "; no recorded interval means unmeasured.")}</p>
            <div className="button-row">
              {!recording && <button type="button" className="button small" disabled={!!startedAt || draft.active_intervals.length >= 100} onClick={() => { update({ timerWasRunning: true }); setStartedAt(new Date().toISOString()); setTimerError(""); }}>{t("开始主动工作计时", "Start active-time recording")}</button>}
              <button type="button" className="button small" disabled={!startedAt} onClick={pause}>{t("暂停主动工作计时", "Pause active-time recording")}</button>
            </div>
            {draft.active_intervals.map((interval, index) => <p className="fine-print" key={index}>{interval.started_at} → {interval.ended_at}</p>)}
            <p className="fine-print">{t("仅保存显式区间；这不是完整研究耗时或节省时间的证明，后台等待不会自动扣除。保存前必须暂停。", "Only explicit intervals are saved. They do not establish total research time or time savings; waiting is not automatically deducted. Pause before saving.")}</p>
          </details>}
        </fieldset>}
      </details>
      {startedAt && <p role="status">{t("审核主动计时正在进行；请展开记录设置并暂停后保存。", "Active review timing is running. Open recording settings and pause it before saving.")}</p>}
      {timerError && <p role="alert">{timerError}</p>}
      <div className="result-review-submit"><span className="fine-print">{t("保存形成针对本次冻结结果的服务端审核记录。", "Saving creates a server review record for this frozen result.")}</span><button className="button primary" disabled={!value.verdict || !value.category || !value.note.trim() || !!startedAt} type="submit">{t("保存审核", "Save review")}</button></div>
    </fieldset>
  </form>;
}
