import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { canReview, describeError, compatibleCases, formatDate, modeName, regressionOutcome, shortId, statusName } from "../domain";
import type { RegressionCase, RegressionCheck, Research, Run, Review } from "../domain";
import { Badge, Empty, Field, JsonDetails } from "./ui";
import { sourceNames } from "../feedback";
import { retainSelected, revisionLabel, selectedResearchRun } from "../dailyWorkflow";
import ResearchReviewForm from "./ResearchReviewForm";
import { assessmentDimensions, semanticStatusNames } from "../researchAssessment";
import { recoveryKey } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { useI18n } from "../i18n";
import MutationRecovery from "./MutationRecovery";
import "./resultReview.css";

const dimensionEnglish: Record<string, string> = { evidence_accuracy: "Quotation and page accuracy", hypothesis_fidelity: "Fidelity to the cited source", mechanism_attribution: "Economic explanation attribution", field_semantics: "Field semantics and substitutions", implementation_alignment: "Implementation and formula alignment" };
const sourceEnglish: Record<string, string> = { human: "Human review", automation: "Agent or automated record", imported: "Imported record", legacy_unknown: "Legacy source not recorded" };
const semanticEnglish: Record<string, string> = { passed: "Detailed assessment passed (human declaration)", failed: "Some dimensions failed", incomplete: "Assessment incomplete", not_assessed: "No detailed assessment", not_human: "Non-human assessment record" };

export default function QualityPanel({
  workspaceId, research, runs, run: incomingRun, selectedRun, artifacts, cases, checks, busy,
  onSelectRun, onPerform, refresh, onCaseCreated, recording = false, onReviewTimerRunningChange, onReviewDirtyChange, onReport, onFollowReview,
}: {
  workspaceId: string; research: Research; runs: Run[]; run: Run | null; selectedRun: string;
  artifacts: import("../domain").Artifact[]; cases: RegressionCase[]; checks: RegressionCheck[]; busy: string;
  onSelectRun: (id: string) => void; onPerform: (label: string, action: () => Promise<void>) => Promise<void>;
  refresh: () => Promise<void>; onCaseCreated?: (value: RegressionCase) => void; recording?: boolean;
  onReviewTimerRunningChange?: (running: boolean) => void; onReviewDirtyChange?: (dirty: boolean) => void; onReport?: () => void; onFollowReview?: (review: Review) => void;
}) {
  const { t } = useI18n();
  const run = selectedResearchRun(research.id, selectedRun, incomingRun), candidates = run?.state?.candidates || [];
  const [selectedCases, setSelectedCases] = useState<string[]>([]), [checkNotice, setCheckNotice] = useState("");
  const selectedRunRef = useRef(selectedRun); selectedRunRef.current = selectedRun;
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const checkMutation = useRecoverableMutation<RegressionCheck>(recoveryKey(workspaceId, "regression-request", research.id, selectedRun), "/regression-checks");
  useEffect(() => { setSelectedCases([]); setCheckNotice(""); }, [selectedRun]);
  const options = retainSelected(runs, run?.research_id === research.id ? run : null), reviews = run?.reviews || [];
  const eligible = compatibleCases(cases, research, candidates), selectedChecks = checks.filter(c => runs.some(r => r.id === c.run_id));
  const canSubmit = !!run && canReview(run), researchCases = cases.filter(c => c.research_id === research.id);
  const checkBlocked = !!busy || !!checkMutation.pending || !checkMutation.ready || checkMutation.blocked;
  async function submitCheck(body?: Record<string, unknown>) {
    const originalRun = selectedRun;
    await onPerform(body ? t("执行回归检查", "Run regression check") : t("安全重试回归检查", "Safely retry regression check"), async () => {
      const checked = await checkMutation.execute(body);
      if (!mounted.current || selectedRunRef.current !== originalRun) return;
      let warning = "";
      try { checkMutation.acknowledge(); } catch (error) { warning = t("本机请求尚未清理，请安全重试。", "The local request could not be cleared. Safely retry. ") + describeError(error); }
      if (selectedRunRef.current === originalRun) { setSelectedCases([]); setCheckNotice(t("回归检查 ", "Regression check ") + checked.id + t(" 已保存。", " saved. ") + warning); }
      try { await refresh(); } catch (error) { if (mounted.current && selectedRunRef.current === originalRun) setCheckNotice(t("回归检查已保存，列表刷新暂未成功。", "The regression check was saved, but the list could not refresh. ") + describeError(error) + warning); }
    });
  }
  return <div className="result-review">
    <section className="panel quality-intro">
      <div><h2>{t("核对输出，再记录判断", "Inspect the output, then decide")}</h2><p>{t("选择实验后，先查看冻结原文、实现与计算结果，再保存本次判断。引文匹配和产物校验不能替代研究语义审核。", "Choose a run, compare the frozen source, implementation and computed output, then save your judgment. Quote matching and artifact verification do not establish research fidelity.")}</p></div>
      <Field label={t("选择已结束的实验", "Select a finished run")}><select value={selectedRun} onChange={e => onSelectRun(e.target.value)}>
        <option value="">{t("选择实验", "Choose a run")}</option>{options.map(r => <option key={r.id} value={r.id}>{shortId(r.id)} · {revisionLabel(research, r.revision_id)} · {modeName(r.mode)} · {statusName(r.status)}</option>)}
      </select></Field>
    </section>
    {run && <section className="info-note result-frozen-context" aria-label={t("本次审核的冻结版本", "Frozen revision for this review")}>
      <p>{t("最新保存：", "Latest saved: ")}{revisionLabel(research, research.latest_revision_id)} · {t("所选实验：", "Selected run: ")}{shortId(run.id)} · {t("实验冻结：", "Run revision: ")}{revisionLabel(research, run.revision_id)}</p>
      {run.revision_id !== research.latest_revision_id && <p>{t("你正在审核历史版本。本页依据与输出始终属于所选实验，不代表最新保存版本的效果。", "You are reviewing a historical revision. This evidence and output belong to the selected run and do not represent the latest saved revision.")}</p>}
      <JsonDetails value={{ research_id: research.id, latest_revision_id: research.latest_revision_id, run_id: run.id, frozen_revision_id: run.revision_id }} label={t("查看研究与实验的完整标识", "View full research and run identities")}/>
    </section>}
    <section className="panel review-main-panel">
      <div className="panel-heading"><h2>{t("审核冻结结果", "Review frozen results")}</h2></div>
      {!canSubmit && <div className="info-note">{t("请选择已完成产物校验的终态实验，再添加审核。已发送的原请求仍可安全确认，不代表当前产物重新通过校验。", "Select a terminal run whose artifacts passed verification before adding a review. An original submitted request can still be confirmed; replay does not re-verify current artifacts.")}</div>}
      {run && <ResearchReviewForm key={workspaceId + ":" + run.id} workspaceId={workspaceId} run={run} allowNewReview={canSubmit}
        candidates={candidates} paperId={research.paper_id} artifacts={artifacts} busy={busy} recording={recording}
        onTimerRunningChange={onReviewTimerRunningChange} onReviewDirtyChange={onReviewDirtyChange} onPerform={onPerform} refresh={refresh}/>}
      {!run && <Empty title={t("尚无选定的审核结果", "No review result selected")}>{t("先选择一个实验；本页不会将其他实验的输出当作当前依据。", "Choose a run first. Output from another run is never used as the current evidence.")}</Empty>}
      <div className="review-history">
        {reviews.length > 0 && <h3>{t("已保存的审核判断", "Saved review judgments")}</h3>}
        {reviews.map(review => {
          const semantic = review.assessment_summary?.semantic_status || "not_assessed", source = review.source || "legacy_unknown";
          return <article key={review.id}>
            <div className="result-section-heading"><strong>{review.candidate_id}</strong><Badge status={review.verdict}/></div>
            <p>{review.note}</p><p>{t(semanticStatusNames[semantic] || semantic, semanticEnglish[semantic] || semantic)}</p>
            {review.assessment && <>
              <p className="fine-print">{t("审核人（声明）：", "Reviewer (declared): ")}{review.assessment.reviewer} · {t("主动工作时间：", "Active time: ")}{review.assessment_summary?.timing_recorded ? review.assessment_summary.total_active_seconds.toFixed(2) + t(" 秒（显式记录）", " seconds (explicit intervals)") : t("未记录", "Not recorded")}</p>
              <div className="table-scroll"><table><thead><tr><th>{t("研究维度", "Research dimension")}</th><th>{t("判断", "Verdict")}</th><th>{t("理由", "Rationale")}</th></tr></thead>
                <tbody>{Object.entries(review.assessment.dimensions).map(([key, value]) => <tr key={key}><th scope="row">{t(assessmentDimensions[key as keyof typeof assessmentDimensions], dimensionEnglish[key] || key)}</th>
                  <td>{{ passed: t("通过", "Passed"), failed: t("未通过", "Failed"), not_assessed: t("未评定", "Not assessed"), not_applicable: t("不适用", "Not applicable") }[value.outcome]}</td><td>{value.reason}</td></tr>)}</tbody></table></div>
            </>}
            <small>{formatDate(review.created_at)} · {t("结果 ", "Result ")}{shortId(review.result_digest)} · {t(sourceNames[source], sourceEnglish[source] || source)} · {t("尝试 ", "Attempt ")}{shortId(review.attempt_id)}</small>
            {review.verdict==="needs_changes"&&onFollowReview&&<div className="button-row"><button type="button" className="button primary" disabled={!!busy} onClick={()=>onFollowReview(review)}>{review.category==="evidence"?t("补充这条意见的论文证据","Update evidence for this review"):t("根据这条意见创建修订","Revise from this review")}</button></div>}
            <JsonDetails value={review} label={t("查看审核目标与分项记录", "View review target and assessment records")}/>
          </article>;
        })}
      </div>
      {onReport && reviews.length > 0 && <div className="button-row"><button className="button" disabled={!canSubmit} onClick={onReport}>{t("继续为当前实验生成报告", "Continue to this run report")}</button></div>}
    </section>
    <details className="review-development-tools">
      <summary>{t("可选：回归验证与开发检查", "Optional: regression and development checks")}</summary>
      <p className="fine-print">{t("这些操作用于后续软件回归和开发验证，不是保存审核的必经步骤。", "These operations support software regression and development validation. They are not required to save a review.")}</p>
      <section className="panel"><h2>{t("批准开发回归用例", "Approve a development regression case")}</h2>
        <p className="muted">{t("手动填写预期状态。批准时冻结所审核版本的假设、公式、数据与评估条件；数值由独立参考实现检查，当前输出不会自动成为正确答案。", "Choose the expected status independently. Approval freezes the reviewed hypothesis, expression, data and evaluation settings. Numerical checks use an independent reference; current output does not automatically become the expected answer.")}</p>
        <CaseApprovalForm key={workspaceId + ":" + research.id + ":" + selectedRun} workspaceId={workspaceId}
          researchId={research.id} runId={selectedRun} reviews={reviews} busy={busy} onPerform={onPerform} refresh={refresh} onCaseCreated={onCaseCreated}/>
      </section>
      <section className="panel regression-panel">
        <div className="panel-heading"><h2>{t("检查下一次实验", "Check a subsequent run")}</h2>
          <button className="button primary small" disabled={checkBlocked || !canSubmit || !run?.regression_target || !selectedCases.length}
            onClick={() => void submitCheck({ run_id: run!.id, case_ids: selectedCases, expected_attempt_id: run!.regression_target!.attempt_id, expected_result_digest: run!.regression_target!.result_digest })}>{t("执行回归检查", "Run regression check")}</button>
        </div>
        {checkNotice && <p role="status">{checkNotice}</p>}
        <MutationRecovery name={t("回归检查", "regression check")} pending={checkMutation.pending} error={checkMutation.error} busy={!!busy} onRetry={() => void submitCheck()} onDismiss={checkMutation.dismissRejected}/>
        <p className="muted">{t("下方只按研究、论文、数据及候选标识筛选。提交后由后端比较冻结条件；条件改变时记录“不可比较”，检查结果永久保存。", "The list is filtered by research, paper, data and candidate identities. The server compares frozen settings after submission. Changed conditions are recorded as not comparable; check results are persisted.")}</p>
        {researchCases.length ? <div className="table-scroll"><table><thead><tr>
          <th>{t("选择", "Select")}</th><th>{t("候选", "Candidate")}</th><th>{t("人工预期", "Declared expectation")}</th><th>{t("批准说明", "Approval note")}</th><th>{t("适用条件", "Conditions")}</th>
        </tr></thead><tbody>{researchCases.map(c => {
          const compatible = eligible.some(e => e.id === c.id);
          return <tr key={c.id}><td><input type="checkbox" aria-label={t("选择回归用例 ", "Select regression case ") + c.candidate_id}
            checked={selectedCases.includes(c.id)} disabled={!compatible || checkBlocked} onChange={e => setSelectedCases(old => e.target.checked ? [...old, c.id] : old.filter(id => id !== c.id))}/></td>
            <td><strong>{c.candidate_id}</strong><small className="cell-subtitle">v{c.version} · {shortId(c.id)}</small></td><td><Badge status={c.expected_status}/></td>
            <td className="note-cell">{c.note}{c.contract && <JsonDetails value={c.contract} label={t("冻结条件 · ", "Frozen conditions · ") + shortId(c.contract_digest ?? undefined)}/>}</td>
            <td>{!c.contract || !c.contract_digest ? t("旧用例未冻结条件；请从原审核重新批准新用例", "This legacy case has no frozen conditions. Approve a new case from the original review.")
              : compatible ? t("标识匹配；条件待后端确认", "Identities match; conditions await server verification") : t("当前实验标识不匹配", "Current run identities do not match")}</td></tr>;
        })}</tbody></table></div> : <Empty title={t("暂无批准的回归用例", "No approved regression cases")}>{t("完成一次审核，并独立指定预期状态后，这里会出现可重复使用的检查。", "Complete a review and independently declare an expected status to create a reusable check.")}</Empty>}
      </section>
      <section className="panel"><div className="panel-heading"><h2>{t("已保存的回归结果", "Saved regression results")}</h2><span className="count-pill">{selectedChecks.length}</span></div>
        <p className="muted">{t("字面引用核验与独立数值核验分别记录在检查项中；这些结果不代替上方的人工语义判断。", "Literal quote and independent numerical verification are separate checks. Neither replaces human semantic judgment.")}</p>
        {selectedChecks.map(c => <article className="saved-check" key={c.id}>
          <div className="candidate-top"><div><strong>{t("实验 ", "Run ")}{shortId(c.run_id)}</strong><span className="muted"> · {formatDate(c.created_at)}</span></div>
            <span className={"badge " + (regressionOutcome(c) === "passed" ? "status-evaluated" : regressionOutcome(c) === "failed" ? "status-failed" : "")}>{regressionOutcome(c) === "passed" ? t("声明范围内通过", "Passed within declared scope") : regressionOutcome(c) === "failed" ? t("存在不符合预期", "Some checks differ from expectations") : t("存在不可比较项", "Some checks are not comparable")}</span>
          </div>
          {c.results.map((result, i) => <div key={result.case_id + "-" + i}>
            <div className="check-result"><span className={regressionOutcome(result) === "passed" ? "text-green" : regressionOutcome(result) === "failed" ? "text-red" : "muted"}>{statusName(regressionOutcome(result))}</span>
              <strong>{result.candidate_id}</strong><span>{t("状态预期 ", "Expected status ")}{statusName(result.expected_status)}</span><span>{t("状态实际 ", "Observed status ")}{result.actual_status ? statusName(result.actual_status) : t("未比较", "Not compared")}</span>
              {result.reason && <small>{result.reason}</small>}
            </div>
            {!!result.differences?.length && <p className="fine-print">{t("条件差异：", "Condition differences: ")}{result.differences.join("; ")}</p>}
            {!!result.checks?.length && <details><summary>{t("查看逐项检查", "View individual checks")} ({result.checks.length})</summary><ul>{result.checks.map((check, index) => <li key={check.name + "-" + index}>
              <strong>{check.name}</strong> · {statusName(check.outcome)}<p>{check.reason}</p>
              {(check.expected !== undefined || check.actual !== undefined) && <JsonDetails value={{ expected: check.expected, actual: check.actual }} label={t("查看预期与实际值", "View expected and observed values")}/>}
              <JsonDetails value={check} label={t("查看完整检查记录", "View the full check record")}/>
            </li>)}</ul></details>}
          </div>)}
          <p className="fine-print">{t("检查范围：", "Check scope: ")}{c.scope}{!c.outcome && t("。历史记录仅检查状态，未执行新的冻结条件与数值检查", ". This historical record only checked status; it did not apply the new frozen-condition or numerical checks")}{t("。不代表经济假设已获验证或投资有效性。", ". This does not establish an economic hypothesis or investment validity.")}</p>
        </article>)}
        {!selectedChecks.length && <p className="muted">{t("执行检查后，可在这里比较各版本是否保持预期行为。", "After running a check, compare whether successive revisions retain the expected behavior here.")}</p>}
      </section>
    </details>
  </div>;
}
function CaseApprovalForm({ workspaceId, researchId, runId, reviews, busy, onPerform, refresh, onCaseCreated }: {
  workspaceId: string; researchId: string; runId: string; reviews: Review[]; busy: string;
  onPerform: (label: string, action: () => Promise<void>) => Promise<void>; refresh: () => Promise<void>; onCaseCreated?: (value: RegressionCase) => void;
}) {
  const { t } = useI18n();
  const [reviewId, setReviewId] = useState(""), [expected, setExpected] = useState(""), [note, setNote] = useState(""), [notice, setNotice] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const mutation = useRecoverableMutation<RegressionCase>(recoveryKey(workspaceId, "case-request", researchId, runId), "/regression-cases");
  async function save(body?: Record<string, unknown>) {
    await onPerform(body ? t("批准回归用例", "Approve regression case") : t("安全重试回归批准", "Safely retry case approval"), async () => {
      const created = await mutation.execute(body);
      let localWarning = "";
      try { mutation.acknowledge(); } catch (e) { localWarning = t("本机恢复记录尚未清理；原请求已保留，请安全重试完成确认。", "The local recovery record could not be cleared. The original request is retained; safely retry to confirm. ") + describeError(e); }
      if (mounted.current) { setReviewId(""); setExpected(""); setNote(""); setNotice(t("回归用例已保存。", "Regression case saved. ") + localWarning); onCaseCreated?.(created); }
      try { await refresh(); } catch (e) { if (mounted.current) setNotice(t("回归用例已保存，列表刷新暂未成功；请刷新查看记录。", "The case was saved, but the list could not refresh. Refresh to see the record. ") + describeError(e) + localWarning); }
    });
  }
  async function submit(e: FormEvent) { e.preventDefault(); await save({ review_id: reviewId, expected_status: expected, note: note.trim() }); }
  return <form onSubmit={submit}>
    {notice && <p role="status">{notice}</p>}{mutation.error && <p role="alert">{mutation.error}</p>}
    {mutation.pending && <section aria-label={t("回归批准提交恢复", "Recover regression approval")} className="info-note">
      <p>{mutation.pending.state === "rejected" ? t("批准请求已明确拒绝，可以解除后修改。", "The approval request was explicitly rejected. Release it before editing.") : t("回归批准结果待确认。请保留原请求并安全重试。", "Regression approval is unconfirmed. Keep and safely retry the original request.")}</p>
      <JsonDetails value={mutation.pending.body} label={t("查看冻结批准请求", "View the frozen approval request")}/>
      {mutation.pending.state === "rejected" ? <button type="button" className="button" disabled={!!busy} onClick={mutation.dismissRejected}>{t("解除被拒绝的批准请求", "Release rejected approval request")}</button> : <button type="button" className="button" disabled={!!busy} onClick={() => void save()}>{t("安全重试回归批准", "Safely retry case approval")}</button>}
    </section>}
    <fieldset disabled={!!busy || !!mutation.pending || mutation.blocked || !mutation.ready}>
      <Field label={t("引用人工审核", "Reference a saved review")}><select required value={reviewId} onChange={e => setReviewId(e.target.value)}><option value="">{t("选择已保存的审核", "Choose a saved review")}</option>{reviews.map(r => <option key={r.id} value={r.id}>{r.candidate_id} · {statusName(r.verdict)} · {shortId(r.id)}</option>)}</select></Field>
      <Field label={t("预期候选状态", "Expected candidate status")}><select required value={expected} onChange={e => setExpected(e.target.value)}><option value="">{t("请独立选择预期状态", "Choose the expected status independently")}</option>{["evaluated", "blocked", "not_evaluable", "failed", "rejected", "budget_stopped"].map(s => <option key={s} value={s}>{statusName(s)} ({s})</option>)}</select></Field>
      <Field label={t("批准理由", "Approval rationale")}><textarea rows={4} required value={note} onChange={e => setNote(e.target.value)} placeholder={t("为什么这个状态是正确行为？适用条件是什么？", "Why is this the correct behavior, and under which conditions?")}/></Field>
      <button className="button" disabled={!reviewId || !expected || !note.trim()} type="submit">{t("明确批准为回归用例", "Explicitly approve as a regression case")}</button>
    </fieldset>
  </form>;
}
