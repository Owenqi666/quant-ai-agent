import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import { describeError, formatDate, shortId, statusName } from "../domain";
import type { RegressionCheck, Research, Review, Run } from "../domain";
import { recoveryKey } from "../reviewRecovery";
import { issueDecisionPath } from "../taskRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { dispositions, issueStates, sourceNames, validateDecision } from "../feedback";
import type { FeedbackSummary, Issue, Ratio } from "../feedback";
import { Field, JsonDetails } from "./ui";
import CatalogSelect from "./CatalogSelect";
import MutationRecovery from "./MutationRecovery";

export default function FeedbackPanel({ workspaceId, research, reviews, runs, checks, busy, initialIssueId, onPerform, refresh }: {
  workspaceId:string;research: Research; reviews: Review[]; runs: Run[]; checks: RegressionCheck[]; busy: string; initialIssueId?: string;
  onPerform: (label: string, action: () => Promise<void>) => Promise<void>; refresh: () => Promise<void>;
}) {
  const [reviewId, setReviewId] = useState("");
  const [note, setNote] = useState("");
  const [source, setSource] = useState("human");
  const [disposition, setDisposition] = useState("implementation_fix");
  const [issueId, setIssueId] = useState(initialIssueId || "");
  const [issue, setIssue] = useState<Issue | null>(null);
  const [state, setState] = useState("proposed");
  const [decisionNote, setDecisionNote] = useState("");
  const [revisionId, setRevisionId] = useState("");
  const [runId, setRunId] = useState("");
  const [checkId, setCheckId] = useState("");
  const [summary, setSummary] = useState<FeedbackSummary | null>(null);
  const [issueError, setIssueError] = useState("");
  const [notice,setNotice]=useState("");
  const mounted=useRef(true),selectedIssue=useRef(issueId);selectedIssue.current=issueId;
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  const createMutation=useRecoverableMutation<Issue>(recoveryKey(workspaceId,"issue-create-request",research.id),"/issues");
  const decisionKey=recoveryKey(workspaceId,"issue-event-request",research.id);
  let decisionPath=`/issues/${encodeURIComponent(issue?.id || "unselected")}/events`;
  try{decisionPath=issueDecisionPath(localStorage,decisionKey,issue?.id || "");}catch{/* The hook reports blocked browser storage without crashing the form. */}
  const decisionMutation=useRecoverableMutation<Issue>(decisionKey,decisionPath);
  const mutationBlocked=!!createMutation.pending||!!decisionMutation.pending||createMutation.blocked||decisionMutation.blocked||!createMutation.ready||!decisionMutation.ready;
  function show(value: Issue) {
    setIssue(value); setIssueId(value.id);
    const latest = value.events.at(-1);
    setState(value.state); setDisposition(latest?.disposition || "implementation_fix");
    setRevisionId(latest?.revision_id || ""); setRunId(latest?.target_run_id || ""); setCheckId(latest?.check_id || "");
    setDecisionNote("");
  }
  useEffect(() => {
    if (!initialIssueId) return;
    const abort = new AbortController();
    setIssueId(initialIssueId);
    setIssueError("");
    void api<Issue>(`/issues/${encodeURIComponent(initialIssueId)}`, { signal: abort.signal })
      .then((value) => { if (!abort.signal.aborted) show(value); })
      .catch((error) => { if (!abort.signal.aborted) setIssueError(error instanceof Error ? error.message : String(error)); });
    return () => abort.abort();
  }, [initialIssueId]);
  async function create(body?:Record<string,unknown>) {
    await onPerform(body?"记录待处理问题":"安全重试问题创建", async () => {
      const value = await createMutation.execute(body);
      if(!mounted.current)return;
      let warning="";
      try{createMutation.acknowledge();}catch(error){warning=`本机原请求尚未清理，请安全重试。${describeError(error)}`;}
      show(value);setNote("");setNotice(`问题 ${value.id} 已保存。${warning}`);
      try{await refresh();}catch(error){if(mounted.current)setNotice(`问题 ${value.id} 已保存，列表刷新暂未成功。${describeError(error)}${warning}`);}
    });
  }
  async function update(body?:Record<string,unknown>) {
    await onPerform(body?"追加问题处理记录":"安全重试问题处理", async () => {
      const owner=body?issue?.id:decisionMutation.pending?.context?.issueId;
      const value=await decisionMutation.execute(body,body?{issueId:owner}:undefined);
      if(!mounted.current)return;
      let warning="";
      try{decisionMutation.acknowledge();}catch(error){warning=`本机原请求尚未清理，请安全重试。${describeError(error)}`;}
      if(!selectedIssue.current||selectedIssue.current===owner)show(value);
      setNotice(`问题 ${value.id} 的处理记录已保存。${warning}`);
      try{await refresh();}catch(error){if(mounted.current)setNotice(`处理记录已保存，列表刷新暂未成功。${describeError(error)}${warning}`);}
    });
  }
  async function submitCreate(e:FormEvent){e.preventDefault();await create({review_id:reviewId.trim(),note:note.trim(),disposition,source});}
  async function submitUpdate(e:FormEvent){
    e.preventDefault();if(!issue)return;
    try{validateDecision(state,disposition,decisionNote,revisionId,runId,checkId);setIssueError("");}
    catch(error){setIssueError(describeError(error));return;}
    await update({base_event_id:issue.latest_event_id,state,disposition,note:decisionNote.trim(),source,
      revision_id:revisionId.trim()||null,target_run_id:runId.trim()||null,check_id:checkId.trim()||null});
  }
  const ratioRows: [string, Ratio | undefined][] = [
    ["人工声明审核覆盖", summary?.metrics.human_review_coverage], ["任意来源审核覆盖", summary?.metrics.any_review_coverage],
    ["问题闭环", summary?.metrics.issue_closure], ["人工来源问题闭环", summary?.metrics.human_issue_closure],
    ["同条件修复核验", summary?.metrics.same_condition_fix_verification],
  ];
  return <>
    <section className="panel">
      <h2>将审核关联到修改与复测</h2>
      {notice&&<p role="status">{notice}</p>}
      <MutationRecovery name="问题创建" pending={createMutation.pending} error={createMutation.error} busy={!!busy}
        onRetry={()=>void create()} onDismiss={createMutation.dismissRejected}/>
      <MutationRecovery name="问题处理" pending={decisionMutation.pending} error={decisionMutation.error} busy={!!busy}
        onRetry={()=>void update()} onDismiss={decisionMutation.dismissRejected}/>
      <p className="muted">每次处理都追加记录。状态不会因实验通过自动关闭；变更假设或数据需要明确标记，不能当作同条件实现修复。</p>
      <fieldset disabled={mutationBlocked}>
      <Field label="问题处理声明来源" hint="来源声明不验证人员身份；自动浏览器验收须选自动验收样例。"><select value={source} onChange={(e) => setSource(e.target.value)}>{Object.entries(sourceNames).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></Field>
      <Field label="问题处理类型"><select value={disposition} onChange={(e) => setDisposition(e.target.value)}>{Object.entries(dispositions).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></Field>
      <form onSubmit={submitCreate}>
        <CatalogSelect<Review> label="选择原审核" resource="reviews" researchId={research.id} value={reviewId} onChange={setReviewId} seed={reviews} refreshKey={reviews.map((r) => r.id).join(":")} describe={(review) => `${review.candidate_id} · ${statusName(review.verdict)} · ${sourceNames[review.source || "legacy_unknown"]} · ${formatDate(review.created_at)}`} />
        <details><summary>高级：输入其他历史审核标识</summary><Field label="原审核 ID"><input value={reviewId} onChange={(e) => setReviewId(e.target.value)} /></Field></details>
        <Field label="待处理问题依据"><textarea required value={note} onChange={(e) => setNote(e.target.value)} rows={3} /></Field>
        <button type="submit" className="button" disabled={!!busy || !reviewId.trim() || !note.trim()}>从审核创建问题</button>
      </form>
      <CatalogSelect<Issue> label="选择已记录问题" resource="issues" researchId={research.id} value={issueId} onChange={setIssueId} seed={issue?.research_id === research.id ? [issue] : []} refreshKey={issue?.latest_event_id || ""} describe={(row) => `${row.candidate_id} · ${issueStates[row.state]} · ${formatDate(row.created_at)}`} />
      <details><summary>高级：输入历史问题标识</summary><Field label="已记录问题 ID"><input value={issueId} onChange={(e) => setIssueId(e.target.value)} /></Field></details>
      <button className="button small" disabled={!!busy || !issueId.trim()} onClick={() => void onPerform("读取问题", async () => show(await api<Issue>(`/issues/${encodeURIComponent(issueId.trim())}`)))}>读取最新问题记录</button>
      {issueError && <p className="info-note warning" role="alert">{issueError}</p>}
      {issue && <>
        <h3>问题 {shortId(issue.id)} · {issueStates[issue.state]}</h3>
        <p>原审核 {issue.review_id} · 候选 {issue.candidate_id} · 来源 {sourceNames[issue.source] || issue.source}</p>
        {issue.research_id !== research.id && <p className="info-note warning">该问题属于研究 {issue.research_id}；当前下方汇总仍仅针对当前研究，请检查关联 ID。</p>}
        <form onSubmit={submitUpdate}>
          <Field label="追加处理状态"><select value={state} onChange={(e) => setState(e.target.value)}>{Object.entries(issueStates).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></Field>
          <Field label="选择关联修订"><select value={revisionId} onChange={(e) => { setRevisionId(e.target.value); setRunId(""); setCheckId(""); }}><option value="">请选择研究版本</option>{revisionId && !research.revisions?.some((r) => r.id === revisionId) && <option value={revisionId}>其他研究修订 {shortId(revisionId)}</option>}{research.revisions?.map((revision) => <option key={revision.id} value={revision.id}>v{revision.number} · {revision.note || "初始定义"} · {formatDate(revision.created_at)}</option>)}</select></Field>
          <CatalogSelect<Run> label="选择目标实验" resource="runs" researchId={research.id} value={runId} seed={runs} refreshKey={runs.map((r) => `${r.id}:${r.status}`).join(":")} filter={(r) => !revisionId || r.revision_id === revisionId} onChange={(id, row) => { setRunId(id); setCheckId(""); if (row) setRevisionId(row.revision_id); }} describe={(row) => `v${research.revisions?.find((r) => r.id === row.revision_id)?.number || "?"} · ${statusName(row.status)} · ${row.mode} · ${formatDate(row.created_at)}`} />
          <p className="fine-print">先选择目标实验，再选择该实验对应的回归检查。</p>
          <CatalogSelect<RegressionCheck> label="选择目标回归检查" resource="regression-checks" researchId={research.id} value={checkId} seed={checks} refreshKey={checks.map((c) => c.id).join(":")} filter={(c) => !!runId && c.run_id === runId} onChange={(id) => setCheckId(id)} describe={(row) => `${statusName(row.outcome || (row.passed ? "passed" : "failed"))} · 实验 ${shortId(row.run_id)} · ${formatDate(row.created_at)}`} />
          <details><summary>高级：关联其他研究的完整标识</summary>
            <Field label="关联修订 ID"><input value={revisionId} onChange={(e) => { setRevisionId(e.target.value); setRunId(""); setCheckId(""); }} /></Field>
            <Field label="目标实验 ID"><input value={runId} onChange={(e) => { setRunId(e.target.value); setCheckId(""); }} /></Field>
            <Field label="目标回归检查 ID"><input value={checkId} onChange={(e) => setCheckId(e.target.value)} /></Field>
          </details>
          <p className="fine-print">解决同条件实现问题时，后端会核对目标尝试、结果摘要及原审核批准的回归案例；其他变更仍需关联修订、已验证目标实验和明确判断依据。</p>
          <Field label="本次处理依据"><textarea required value={decisionNote} onChange={(e) => setDecisionNote(e.target.value)} rows={3} /></Field>
          <button type="submit" className="button primary" disabled={!!busy || !decisionNote.trim()}>保存追加处理记录</button>
        </form>
        <div className="table-scroll"><table><thead><tr><th>时间</th><th>状态 / 类型 / 来源</th><th>依据与关联</th></tr></thead><tbody>{issue.events.map((event) => <tr key={event.id}>
          <td>{formatDate(event.created_at)}</td><td>{issueStates[event.state]} / {dispositions[event.disposition]} / {sourceNames[event.source]}</td><td>{event.note}<JsonDetails value={event} label="完整事件与修订、实验、检查 ID" /></td>
        </tr>)}</tbody></table></div>
      </>}
      </fieldset>
    </section>
    <section className="panel">
      <div className="panel-heading"><h2>本研究的审核与问题汇总</h2><button className="button" disabled={!!busy} onClick={() => void onPerform("计算审核汇总", async () => setSummary(await api<FeedbackSummary>(`/feedback-summary?research_id=${research.id}`)))}>计算本研究汇总</button></div>
      <p className="muted">分母按可审核的冻结输出去重，同一输出的重复审核不增加覆盖；自动样例、历史来源未知与人工声明分别统计。关闭历时包含等待，不代表人工工时或节省时间。</p>
      {summary && <>
        <p>生成于 {formatDate(summary.created_at)} · 指标版本 {summary.metrics.metrics_version} · 输入摘要 {shortId(summary.input_digest)}</p>
        <p>{summary.metrics.independent_run_count} 个实验 · {summary.metrics.attempt_count} 个尝试 · {summary.metrics.eligible_output_count} 个可审核输出；另有 {summary.skipped_attempts.length} 个尝试未进入已验证输出集合。</p>
        <div className="table-scroll"><table><thead><tr><th>指标</th><th>分子 / 分母</th><th>比例</th></tr></thead><tbody>{ratioRows.map(([label, ratio]) => ratio && <tr key={label}><td>{label}</td><td>{ratio.numerator} / {ratio.denominator}</td><td>{ratio.rate === null ? "无适用样本" : `${(ratio.rate * 100).toFixed(1)}%`}</td></tr>)}</tbody></table></div>
        {summary.metrics.structured_assessments && <section aria-label="分项审核汇总">
          <h3>分项审核与显式工作时间</h3>
          <p>人工声明的分项审核覆盖：{summary.metrics.structured_assessments.human_output_coverage.numerator} / {summary.metrics.structured_assessments.human_output_coverage.denominator} 个可审核输出。</p>
          <p>记录到的人工主动工作人时：{summary.metrics.structured_assessments.human_active_time.observed_person_seconds === null ? "未记录" : `${summary.metrics.structured_assessments.human_active_time.observed_person_seconds.toFixed(2)} 秒`}。</p>
          <p className="fine-print">同一声明审核人的重叠区间合并计数；不同审核人的区间累计为人时。自动样例不计入人工覆盖或人时。这些是本次审核的显式记录，不是完整研究耗时、身份认证或节省时间的结论。分项与区间明细保存在汇总 JSON。</p>
        </section>}
        <p>{Object.entries(summary.metrics.review_rows_by_source).map(([source, count]) => `${sourceNames[source] || source}：${count}`).join("；")}</p>
        <p>{Object.entries(summary.metrics.issue_states).map(([state, count]) => `${issueStates[state] || state}：${count}`).join("；") || "暂无问题记录"}</p>
        <div className="button-row"><a className="button small" href={`/api/feedback-summary/export?research_id=${research.id}&format=json`} download>导出汇总 JSON</a><a className="button small" href={`/api/feedback-summary/export?research_id=${research.id}&format=csv`} download>导出汇总 CSV</a></div>
        <p className="fine-print">导出时重新计算，文件保留自己的生成时间、过滤条件与输入摘要；新增记录后可能与当前显示快照不同。</p>
        <JsonDetails value={summary} label="检查指标分母、输入记录、未验证尝试及耗时口径" />
      </>}
    </section>
  </>;
}
