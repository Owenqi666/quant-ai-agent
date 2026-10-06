import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  canReview,
  describeError,
  compatibleCases,
  formatDate,
  modeName,
  regressionOutcome,
  shortId,
  statusName,
} from "../domain";
import type {
  RegressionCase,
  RegressionCheck,
  Research,
  Run,
  Review,
} from "../domain";
import { Badge, Empty, Field, JsonDetails } from "./ui";
import { sourceNames } from "../feedback";
import { retainSelected, revisionLabel, selectedResearchRun } from "../dailyWorkflow";
import ResearchReviewForm from "./ResearchReviewForm";
import { assessmentDimensions, semanticStatusNames } from "../researchAssessment";
import { recoveryKey } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import MutationRecovery from "./MutationRecovery";

export default function QualityPanel({
  workspaceId,
  research,
  runs,
  run: incomingRun,
  selectedRun,
  artifacts,
  cases,
  checks,
  busy,
  onSelectRun,
  onPerform,
  refresh,
  onCaseCreated,
  recording = false,
  onReviewTimerRunningChange,
  onReport,
}: {
  workspaceId: string;
  research: Research;
  runs: Run[];
  run: Run | null;
  selectedRun: string;
  artifacts: import("../domain").Artifact[];
  cases: RegressionCase[];
  checks: RegressionCheck[];
  busy: string;
  onSelectRun: (id: string) => void;
  onPerform: (label: string, action: () => Promise<void>) => Promise<void>;
  refresh: () => Promise<void>;
  onCaseCreated?: (value: RegressionCase) => void;
  recording?: boolean;
  onReviewTimerRunningChange?: (running: boolean) => void;
  onReport?: () => void;
}) {
  const run = selectedResearchRun(research.id, selectedRun, incomingRun);
  const candidates = run?.state?.candidates || [];
  const [selectedCases, setSelectedCases] = useState<string[]>([]);
  const [checkNotice,setCheckNotice]=useState("");
  const selectedRunRef=useRef(selectedRun);selectedRunRef.current=selectedRun;
  const mounted=useRef(true);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  const checkMutation=useRecoverableMutation<RegressionCheck>(recoveryKey(workspaceId,"regression-request",research.id,selectedRun),"/regression-checks");
  useEffect(() => { setSelectedCases([]);setCheckNotice(""); }, [selectedRun]);
  const options = retainSelected(runs, run?.research_id === research.id ? run : null);
  const reviews = run?.reviews || [];
  const eligible = compatibleCases(cases, research, candidates);
  const selectedChecks = checks.filter((c) =>
    runs.some((r) => r.id === c.run_id),
  );
  const canSubmit = !!run && canReview(run);
  const researchCases = cases.filter((c) => c.research_id === research.id);
  const checkBlocked=!!busy||!!checkMutation.pending||!checkMutation.ready||checkMutation.blocked;
  async function submitCheck(body?:Record<string,unknown>){
    const originalRun=selectedRun;
    await onPerform(body?"执行回归检查":"安全重试回归检查",async()=>{
      const checked=await checkMutation.execute(body);
      if(!mounted.current||selectedRunRef.current!==originalRun)return;
      let warning="";
      try{checkMutation.acknowledge();}catch(error){warning=`本机请求尚未清理，请安全重试。${describeError(error)}`;}
      if(selectedRunRef.current===originalRun){setSelectedCases([]);setCheckNotice(`回归检查 ${checked.id} 已保存。${warning}`);}
      try{await refresh();}catch(error){if(mounted.current&&selectedRunRef.current===originalRun)setCheckNotice(`回归检查 ${checked.id} 已保存，列表刷新暂未成功。${describeError(error)}${warning}`);}
    });
  }
  return (
    <>
      <section className="panel quality-intro">
        <div>
          <h2>人工审核是回流的起点</h2>
          <p>
            核对当前实验的原文、实现与结果后保存审核，再继续生成报告。回归用例批准和流程计时均为可选；引文匹配不能替代研究语义审核。
          </p>
        </div>
        <Field label="选择已结束的实验">
          <select
            value={selectedRun}
            onChange={(e) => onSelectRun(e.target.value)}
          >
            <option value="">选择实验</option>
            {options.map((r) => (
              <option key={r.id} value={r.id}>
                {shortId(r.id)} · {revisionLabel(research, r.revision_id)} · {modeName(r.mode)} · {statusName(r.status)}
              </option>
            ))}
          </select>
        </Field>
      </section>
      {onReport && <div className="button-row"><button className="button" onClick={onReport}>继续为当前实验生成报告</button></div>}
      <div className="review-workflow">
        <section className="panel">
          <div className="panel-heading">
            <h2>
              <span className="step-number">01</span> 审核冻结结果
            </h2>
          </div>
          {!canSubmit && <div className="info-note">
            请选择已完成产物校验的终态实验，再添加审核。已发送的原请求仍可安全确认，不代表当前产物重新通过校验。
          </div>}
          {run && <ResearchReviewForm key={`${workspaceId}:${run.id}`} workspaceId={workspaceId} run={run} allowNewReview={canSubmit}
            candidates={candidates} paperId={research.paper_id} artifacts={artifacts} busy={busy} recording={recording} onTimerRunningChange={onReviewTimerRunningChange} onPerform={onPerform} refresh={refresh} />}
          <div className="review-history">
            {reviews.map((r) => (
              <article key={r.id}>
                <div>
                  <strong>{r.candidate_id}</strong>
                  <Badge status={r.verdict} />
                </div>
                <p>{r.note}</p>
                <p>{semanticStatusNames[r.assessment_summary?.semantic_status || "not_assessed"]}</p>
                {r.assessment && <>
                  <p className="fine-print">审核人（声明）：{r.assessment.reviewer} · 主动工作时间：{r.assessment_summary?.timing_recorded ? `${r.assessment_summary.total_active_seconds.toFixed(2)} 秒（显式记录）` : "未记录"}</p>
                  <div className="table-scroll"><table><thead><tr><th>研究维度</th><th>判断</th><th>理由</th></tr></thead><tbody>{Object.entries(r.assessment.dimensions).map(([key, value]) => <tr key={key}><td>{assessmentDimensions[key as keyof typeof assessmentDimensions]}</td><td>{{ passed: "通过", failed: "未通过", not_assessed: "未评定", not_applicable: "不适用" }[value.outcome]}</td><td>{value.reason}</td></tr>)}</tbody></table></div>
                </>}
                <small>
                  {formatDate(r.created_at)} · 结果 {shortId(r.result_digest)}
                  {" · "}{sourceNames[r.source || "legacy_unknown"]} · 尝试 {shortId(r.attempt_id)}
                </small>
                <JsonDetails value={r} label="查看审核目标与分项记录" />
              </article>
            ))}
          </div>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>
              <span className="step-number">02</span> 批准开发回归用例
            </h2>
          </div>
          <p className="muted">
            手动填写预期状态。批准时冻结所审核版本的假设、公式、数据与评估条件；数值由独立参考实现检查，当前输出不会自动成为正确答案。
          </p>
          <CaseApprovalForm key={`${workspaceId}:${research.id}:${selectedRun}`} workspaceId={workspaceId}
            researchId={research.id} runId={selectedRun} reviews={reviews} busy={busy}
            onPerform={onPerform} refresh={refresh} onCaseCreated={onCaseCreated} />
        </section>
      </div>
      <section className="panel regression-panel">
        <div className="panel-heading">
          <div>
            <h2>
              <span className="step-number">03</span> 检查下一次实验
            </h2>
          </div>
          <button
            className="button primary small"
            disabled={checkBlocked || !canSubmit || !run?.regression_target || !selectedCases.length}
            onClick={() =>void submitCheck({
                  run_id: run!.id,
                  case_ids: selectedCases,
                  expected_attempt_id:run!.regression_target!.attempt_id,
                  expected_result_digest:run!.regression_target!.result_digest,
                })}
          >
            执行回归检查
          </button>
        </div>
        {checkNotice&&<p role="status">{checkNotice}</p>}
        <MutationRecovery name="回归检查" pending={checkMutation.pending} error={checkMutation.error} busy={!!busy}
          onRetry={()=>void submitCheck()} onDismiss={checkMutation.dismissRejected}/>
        <p className="muted">
          下方只按研究、论文、数据及候选标识筛选。提交后由后端比较冻结条件；条件改变时记录“不可比较”，检查结果永久保存。
        </p>
        {researchCases.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>选择</th>
                  <th>候选</th>
                  <th>人工预期</th>
                  <th>批准说明</th>
                  <th>适用条件</th>
                </tr>
              </thead>
              <tbody>
                {researchCases.map((c) => {
                  const compatible = eligible.some((e) => e.id === c.id);
                  return (
                    <tr key={c.id}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`选择回归用例 ${c.candidate_id}`}
                          checked={selectedCases.includes(c.id)}
                          disabled={!compatible||checkBlocked}
                          onChange={(e) =>
                            setSelectedCases((old) =>
                              e.target.checked
                                ? [...old, c.id]
                                : old.filter((id) => id !== c.id),
                            )
                          }
                        />
                      </td>
                      <td>
                        <strong>{c.candidate_id}</strong>
                        <small className="cell-subtitle">
                          v{c.version} · {shortId(c.id)}
                        </small>
                      </td>
                      <td>
                        <Badge status={c.expected_status} />
                      </td>
                      <td className="note-cell">
                        {c.note}
                        {c.contract && (
                          <JsonDetails
                            value={c.contract}
                            label={`冻结条件 · ${shortId(c.contract_digest ?? undefined)}`}
                          />
                        )}
                      </td>
                      <td>
                        {!c.contract || !c.contract_digest
                          ? "旧用例未冻结条件；请从原审核重新批准新用例"
                          : compatible
                            ? "标识匹配；条件待后端确认"
                            : "当前实验标识不匹配"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="暂无批准的回归用例">
            完成一次审核，并独立指定预期状态后，这里会出现可重复使用的检查。
          </Empty>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>已保存的回归结果</h2>
          <span className="count-pill">{selectedChecks.length}</span>
        </div>
        <p className="muted">字面引用核验与独立数值核验分别记录在检查项中；这些结果不代替上方的人工语义判断。</p>
        {selectedChecks.map((c) => (
          <article className="saved-check" key={c.id}>
            <div className="candidate-top">
              <div>
                <strong>实验 {shortId(c.run_id)}</strong>
                <span className="muted"> · {formatDate(c.created_at)}</span>
              </div>
              <span
                className={`badge ${regressionOutcome(c) === "passed" ? "status-evaluated" : regressionOutcome(c) === "failed" ? "status-failed" : ""}`}
              >
                {regressionOutcome(c) === "passed"
                  ? "声明范围内通过"
                  : regressionOutcome(c) === "failed"
                    ? "存在不符合预期"
                    : "存在不可比较项"}
              </span>
            </div>
            {c.results.map((r, i) => (
              <div key={`${r.case_id}-${i}`}>
                <div className="check-result">
                  <span
                    className={
                      regressionOutcome(r) === "passed"
                        ? "text-green"
                        : regressionOutcome(r) === "failed"
                          ? "text-red"
                          : "muted"
                    }
                  >
                    {statusName(regressionOutcome(r))}
                  </span>
                  <strong>{r.candidate_id}</strong>
                  <span>状态预期 {statusName(r.expected_status)}</span>
                  <span>
                    状态实际{" "}
                    {r.actual_status ? statusName(r.actual_status) : "未比较"}
                  </span>
                  {r.reason && <small>{r.reason}</small>}
                </div>
                {!!r.differences?.length && (
                  <p className="fine-print">条件差异：{r.differences.join("；")}</p>
                )}
                {!!r.checks?.length && (
                  <details>
                    <summary>查看逐项检查（{r.checks.length}）</summary>
                    <ul>
                      {r.checks.map((check, index) => (
                        <li key={`${check.name}-${index}`}>
                          <strong>{check.name}</strong> · {statusName(check.outcome)}
                          <p>{check.reason}</p>
                          {(check.expected !== undefined || check.actual !== undefined) && (
                            <JsonDetails
                              value={{ expected: check.expected, actual: check.actual }}
                              label="查看预期与实际值"
                            />
                          )}
                          <JsonDetails value={check} label="查看完整检查记录" />
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            ))}
            <p className="fine-print">
              检查范围：{c.scope}
              {!c.outcome && "。历史记录仅检查状态，未执行新的冻结条件与数值检查"}
              。不代表经济假设已获验证或投资有效性。
            </p>
          </article>
        ))}
        {!selectedChecks.length && (
          <p className="muted">
            执行检查后，可在这里比较各版本是否保持预期行为。
          </p>
        )}
      </section>
    </>
  );
}


function CaseApprovalForm({ workspaceId, researchId, runId, reviews, busy, onPerform, refresh, onCaseCreated }: {
  workspaceId: string; researchId: string; runId: string; reviews: Review[]; busy: string;
  onPerform: (label: string, action: () => Promise<void>) => Promise<void>; refresh: () => Promise<void>;
  onCaseCreated?: (value: RegressionCase) => void;
}) {
  const [reviewId, setReviewId] = useState("");
  const [expected, setExpected] = useState("");
  const [note, setNote] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const mutation = useRecoverableMutation<RegressionCase>(recoveryKey(workspaceId, "case-request", researchId, runId), "/regression-cases");
  async function save(body?: Record<string, unknown>) {
    await onPerform(body ? "批准回归用例" : "安全重试回归批准", async () => {
      const created = await mutation.execute(body);
      let localWarning = "";
      try { mutation.acknowledge(); }
      catch (e) { localWarning = `本机恢复记录尚未清理；原请求已保留，请安全重试完成确认。${describeError(e)}`; }
      if (mounted.current) {
        setReviewId(""); setExpected(""); setNote(""); setNotice(`回归用例已保存。${localWarning}`);
        onCaseCreated?.(created);
      }
      try { await refresh(); }
      catch (e) { if (mounted.current) setNotice(`回归用例已保存，列表刷新暂未成功；请刷新查看记录。${describeError(e)}${localWarning}`); }
    });
  }
  async function submit(e: FormEvent) {
    e.preventDefault();
    await save({ review_id: reviewId, expected_status: expected, note: note.trim() });
  }
  return <form onSubmit={submit}>
    {notice && <p role="status">{notice}</p>}
    {mutation.error && <p role="alert">{mutation.error}</p>}
    {mutation.pending && <section aria-label="回归批准提交恢复" className="info-note">
      <p>{mutation.pending.state === "rejected" ? "批准请求已明确拒绝，可以解除后修改。" : "回归批准结果待确认。请保留原请求并安全重试。"}</p>
      <JsonDetails value={mutation.pending.body} label="查看冻结批准请求" />
      {mutation.pending.state === "rejected" ? <button type="button" className="button" disabled={!!busy} onClick={mutation.dismissRejected}>解除被拒绝的批准请求</button> :
        <button type="button" className="button" disabled={!!busy} onClick={() => void save()}>安全重试回归批准</button>}
    </section>}
    <fieldset disabled={!!busy || !!mutation.pending || mutation.blocked || !mutation.ready}>
      <Field label="引用人工审核"><select required value={reviewId} onChange={(e) => setReviewId(e.target.value)}>
        <option value="">选择已保存的审核</option>{reviews.map((r) => <option key={r.id} value={r.id}>{r.candidate_id} · {statusName(r.verdict)} · {shortId(r.id)}</option>)}
      </select></Field>
      <Field label="预期候选状态"><select required value={expected} onChange={(e) => setExpected(e.target.value)}>
        <option value="">请独立选择预期状态</option>{["evaluated", "blocked", "not_evaluable", "failed", "rejected", "budget_stopped"].map((s) => <option key={s} value={s}>{statusName(s)} ({s})</option>)}
      </select></Field>
      <Field label="批准理由"><textarea rows={4} required value={note} onChange={(e) => setNote(e.target.value)} placeholder="为什么这个状态是正确行为？适用条件是什么？" /></Field>
      <button className="button" disabled={!reviewId || !expected || !note.trim()} type="submit">明确批准为回归用例</button>
    </fieldset>
  </form>;
}
