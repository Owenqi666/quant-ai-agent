import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { api } from "../api";
import { activeStatuses, describeError, shortId, formatDate } from "../domain";
import type { Research, Revision, Review, Run, RegressionCheck } from "../domain";
import { catalogPath, dispositions, issueStates } from "../feedback";
import type { CatalogPage, Issue } from "../feedback";
import { suggestedDisposition, resolutionReviews } from "../reviewFollowup";
import { recoveryKey } from "../reviewRecovery";
import { issueDecisionPath } from "../taskRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { useI18n } from "../i18n";
import { Field } from "./ui";
import MutationRecovery from "./MutationRecovery";
import "./reviewFollowup.css";

export interface ReviewFollowupHandle {
  linkRevision: (revision: Revision) => Promise<void>;
  linkRun: (run: Run) => Promise<void>;
}
interface Props {
  workspaceId: string; navigationKey: string; research: Research; revision?: Revision; selectedIssueId: string; requestedReview: Review | null;
  run: Run | null; checks: RegressionCheck[]; busy: string;
  onSelectIssue: (issue: Issue) => void; onEdit: (issue: Issue) => void;
  onOpenRun: (runId: string) => void; onClear: () => void;
}
const englishDispositions: Record<string, string> = { evidence_update: "Update source evidence", implementation_fix: "Fix implementation under the same conditions", hypothesis_change: "Revise the hypothesis", data_change: "Change the dataset", accept_limitation: "Accept the limitation" };
const englishStates: Record<string, string> = { open: "Open", proposed: "Revision proposed", awaiting_review: "Awaiting review", resolved: "Resolved", deferred: "Deferred" };

/** Guided entry into the existing append-only issue/revision/run/review records. */
export default forwardRef<ReviewFollowupHandle, Props>(function ReviewFollowup({workspaceId,navigationKey,research,revision,selectedIssueId,requestedReview,run,checks,busy,onSelectIssue,onEdit,onOpenRun,onClear}, ref) {
  const { t } = useI18n();
  const [issue,setIssue] = useState<Issue|null>(null), [existing,setExisting] = useState<Issue[]>([]);
  const [loading,setLoading] = useState(false), [error,setError] = useState(""), [notice,setNotice] = useState("");
  const [source,setSource] = useState("human"), [kind,setKind] = useState("evidence_update"), [resolutionId,setResolutionId] = useState(""), [checkId,setCheckId] = useState("");
  const [retry,setRetry] = useState(0), [sending,setSending] = useState(false);
  const mounted=useRef(true), selection=useRef(selectedIssueId), epoch=useRef(0);
  selection.current=selectedIssueId;
  const view=useRef(navigationKey);
  if(view.current!==navigationKey){view.current=navigationKey;epoch.current++;}
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;epoch.current++;};},[]);
  const createMutation=useRecoverableMutation<Issue>(recoveryKey(workspaceId,"guided-issue-create",research.id),"/issues");
  const eventKey=recoveryKey(workspaceId,"guided-issue-event",research.id);
  let eventPath=`/issues/${encodeURIComponent(selectedIssueId || "unselected")}/events`;
  try { eventPath=issueDecisionPath(localStorage,eventKey,selectedIssueId); } catch { /* Mutation hook reports storage errors. */ }
  const eventMutation=useRecoverableMutation<Issue>(eventKey,eventPath);
  const locked=!!busy||sending||loading||!!createMutation.pending||!!eventMutation.pending||createMutation.blocked||eventMutation.blocked||!createMutation.ready||!eventMutation.ready;
  const show=(value:Issue)=>{setIssue(value);onSelectIssue(value);setResolutionId("");setCheckId("");};
  useEffect(()=>{
    const abort=new AbortController(); ++epoch.current;
    setIssue(null);setExisting([]);setError("");setNotice("");setResolutionId("");setCheckId("");
    if (!selectedIssueId && !requestedReview) {setLoading(false);return()=>abort.abort();}
    setLoading(true);
    async function read() {
      if (selectedIssueId) {
        const value=await api<Issue>(`/issues/${encodeURIComponent(selectedIssueId)}`,{signal:abort.signal});
        if (value.research_id!==research.id) throw Error(t("问题不属于当前研究，已停止恢复。","This issue does not belong to the selected study."));
        if (!abort.signal.aborted) {setIssue(value);setSource(value.events.at(-1)?.source||"human");setKind(value.events.at(-1)?.disposition||"evidence_update");}
      } else if (requestedReview) {
        let cursor=0, through:number|undefined; const rows:Issue[]=[];
        for (;;) {
          const page=await api<CatalogPage<Issue>>(catalogPath("issues",{research_id:research.id,review_id:requestedReview.id},cursor,through),{signal:abort.signal});
          rows.push(...page.items);
          if (!page.has_more) break;
          if (page.next_cursor<=cursor) throw Error("Invalid issue catalog cursor");
          cursor=page.next_cursor;through=page.high_watermark;
        }
        if (!abort.signal.aborted) {setExisting(rows);setKind(suggestedDisposition(requestedReview.category));setSource(requestedReview.source==="automation"?"automation":"human");}
      }
    }
    void read().catch(e=>{if(!abort.signal.aborted)setError(describeError(e));}).finally(()=>{if(!abort.signal.aborted)setLoading(false);});
    return()=>abort.abort();
  },[selectedIssueId,requestedReview?.id,research.id,retry]);
  const last=issue?.events.at(-1), original=issue?.original_review || requestedReview;
  const targetRun=run?.id===last?.target_run_id?run:null;
  const accepted=issue?resolutionReviews(issue,targetRun):[];
  const applicableChecks=checks.filter(check=>targetRun&&check.run_id===targetRun.id&&check.passed);
  async function create(replay=false) {
    const token=epoch.current; setSending(true);setError("");
    try {
      if (!replay && (!requestedReview || existing.length || loading || error)) return;
      if(!replay&&(createMutation.pending||eventMutation.pending||createMutation.blocked||eventMutation.blocked||!createMutation.ready||!eventMutation.ready))throw Error(t("请先处理原待确认请求。","Resolve the original pending request first."));
      const value=await createMutation.execute(replay?undefined:{review_id:requestedReview!.id,disposition:kind,note:requestedReview!.note,source});
      if (!mounted.current || epoch.current!==token) return;
      createMutation.acknowledge();show(value);setNotice(t("原意见已关联到处理记录。请在最新版本上创建修订。","The original review is linked. Continue by revising the latest saved version."));
      if (!replay) onEdit(value);
    } catch(e) {if(mounted.current&&epoch.current===token)setError(describeError(e));}
    finally {if(mounted.current)setSending(false);}
  }
  async function decide(body?:Record<string,unknown>) {
    if(body&&(createMutation.pending||eventMutation.pending||createMutation.blocked||eventMutation.blocked||!eventMutation.ready||loading||!issue||issue.id!==selectedIssueId))
      throw Error(t("处理记录尚未就绪或有原请求待确认；未发送新的关联。请先安全确认原请求。","The issue is not ready or an original request is pending. No new link was submitted; confirm the original request first."));
    const token=epoch.current, owner=selectedIssueId;setSending(true);setError("");
    try {
      const value=await eventMutation.execute(body,body?{issueId:owner}:undefined);
      if (!mounted.current || selection.current!==owner) return;
      if(body&&value.id!==owner)throw Error("Issue response does not match the submitted target");
      eventMutation.acknowledge();
      // Confirm a write for the same issue even if selecting its new run changed
      // the view. Only an explicit replay may select an unselected issue.
      if(value.id===selection.current){setIssue(value);setResolutionId("");setCheckId("");}
      else if(!selection.current&&epoch.current===token)show(value);
      setNotice(t("处理进度已保存。","Issue progress saved."));
    } catch(e) {if(mounted.current&&epoch.current===token)setError(describeError(e));throw e;}
    finally {if(mounted.current)setSending(false);}
  }
  const eventBody=(state:string,note:string,revisionId?:string,runId?:string)=>({base_event_id:issue!.latest_event_id,state,disposition:last?.disposition||kind,note,source,revision_id:revisionId||null,target_run_id:runId||null});
  useImperativeHandle(ref,()=>({
    async linkRevision(revision) {
      if(!selectedIssueId)return;
      if(!issue||issue.id!==selectedIssueId||loading)throw Error(t("修订已保存，问题记录尚未就绪；请重新读取后关联。","Revision saved; the issue is not ready. Reload it before linking."));
      if (issue.state==="resolved" || last?.revision_id===revision.id) return;
      await decide(eventBody("proposed",t(`已保存修订 v${revision.number}；尚未运行或复审。`,`Revision v${revision.number} saved; execution and re-review remain pending.`),revision.id));
    },
    async linkRun(saved) {
      if(!selectedIssueId)return;
      if(!issue||issue.id!==selectedIssueId||loading)throw Error(t("实验已提交，问题记录尚未就绪；请重新读取后关联。","Experiment submitted; the issue is not ready. Reload it before linking."));
      if(issue.state==="resolved"||last?.target_run_id===saved.id)return;
      if(last?.revision_id!==saved.revision_id)throw Error(t("实验版本与待处理修订不同，未自动关联。","The run uses a different revision; it was not linked automatically."));
      await decide(eventBody("proposed",t("已明确提交修订实验；等待计算和复审。","Revision experiment explicitly submitted; awaiting computation and review."),saved.revision_id,saved.id));
    },
  }));
  async function openTarget() {
    if(!issue||!last?.target_run_id)return;
    const token=epoch.current,owner=selectedIssueId,targetId=last.target_run_id;
    try {
      if(targetRun&&!activeStatuses.has(targetRun.status)&&targetRun.verification?.verified&&issue.state!=="awaiting_review"&&issue.state!=="resolved") await decide(eventBody("awaiting_review",t("打开已计算的修订结果，准备复审。","Open the computed revision for re-review."),last.revision_id||undefined,last.target_run_id));
      if(!mounted.current||epoch.current!==token||selection.current!==owner)return;
      onOpenRun(targetId);
    } catch { /* Error and durable request remain visible. */ }
  }
  async function closeIssue() {
    if(!issue||!targetRun||!accepted.some(review=>review.id===resolutionId))return;
    try {await decide({...eventBody("resolved",t("依据所选接受审核关闭；原意见与历史输出保留。","Resolve using the selected accepted review; preserve the original review and historical outputs."),targetRun.revision_id,targetRun.id),resolution_review_id:resolutionId,check_id:checkId||null});}catch{/* Keep precise error visible. */}
  }
  if(!selectedIssueId&&!requestedReview&&!createMutation.pending&&!eventMutation.pending&&!createMutation.blocked&&!eventMutation.blocked)return null;
  return <section className="panel review-followup" aria-label={t("审核意见处理","Review follow-up")}>
    <div className="panel-heading"><h2>{t("处理审核意见","Follow up on a review")}</h2><button type="button" className="button small" disabled={sending||!!createMutation.pending||!!eventMutation.pending} onClick={onClear}>{t("收起处理卡","Dismiss follow-up")}</button></div>
    {loading&&<p role="status">{t("读取关联记录…","Loading linked records…")}</p>}
    {error&&<p role="alert">{error} <button type="button" className="button small" disabled={sending} onClick={()=>setRetry(v=>v+1)}>{t("重新读取处理记录","Reload issue records")}</button></p>}
    {notice&&<p role="status">{notice}</p>}
    <MutationRecovery name={t("问题创建","issue creation")} pending={createMutation.pending} error={createMutation.error} busy={sending} onRetry={()=>void create(true)} onDismiss={createMutation.dismissRejected}/>
    <MutationRecovery name={t("问题进度","issue progress")} pending={eventMutation.pending} error={eventMutation.error} busy={sending} onRetry={()=>void decide().catch(()=>{})} onDismiss={eventMutation.dismissRejected}/>
    {original&&<><p><strong>{original.candidate_id}</strong> · {t("原意见：","Original review: ")}{original.note}</p><p className="fine-print">{t("问题来源版本：","Original revision: ")}v{research.revisions?.find(r=>r.id===original.revision_id)?.number||"?"} · {t("编辑基底：","Editing base: ")}v{research.revisions?.find(r=>r.id===research.latest_revision_id)?.number||"?"}</p></>}
    <fieldset disabled={locked||!!error}>
      {(!issue||issue.state!=="resolved")&&<Field label={t("处理记录来源","Decision record source")}><select value={source} onChange={e=>setSource(e.target.value)}><option value="human">{t("人工填写（声明）","Human entry (declared)")}</option><option value="automation">{t("代理或自动化记录","Agent or automated record")}</option></select></Field>}
      {!issue&&requestedReview&&!loading&&(existing.length?<div><p>{t("这条审核已有处理记录，请继续已有问题。","This review already has issue records. Continue an existing one.")}</p>{existing.map(item=><button key={item.id} type="button" className="button" onClick={()=>show(item)}>{t("继续处理","Continue issue")} · {shortId(item.id)} · {t(issueStates[item.state]||item.state,englishStates[item.state]||item.state)}</button>)}</div>:<>
        <Field label={t("本次处理方式","Type of change")}><select value={kind} onChange={e=>setKind(e.target.value)}>{Object.entries(dispositions).map(([id,label])=><option key={id} value={id}>{t(label,englishDispositions[id]||id)}</option>)}</select></Field>
        <button type="button" className="button primary" onClick={()=>void create()}>{t("关联原意见并创建修订","Link this review and revise")}</button>
      </>)}
      {issue&&<>
        <p>{t("处理状态：","Issue status: ")}{t(issueStates[issue.state]||issue.state,englishStates[issue.state]||issue.state)} · {t(dispositions[last?.disposition||""]||"",englishDispositions[last?.disposition||""]||"")}</p>
        {last?.disposition==="evidence_update"&&<p className="fine-print">{t("补充证据后，旧回归条件可能不可比较；新研究依据需要重新审核。","Updated evidence can make the old regression conditions not comparable. Review the new evidence explicitly.")}</p>}
        <div className="button-row">{issue.state!=="resolved"&&<button type="button" className="button" onClick={()=>onEdit(issue)}>{t("继续在最新版本上修订","Continue revising the latest version")}</button>}{last?.target_run_id&&<button type="button" className="button" onClick={()=>void openTarget()}>{t("查看并复审关联实验","Inspect and re-review linked experiment")}</button>}{original&&<button type="button" className="button small" onClick={()=>onOpenRun(original.run_id)}>{t("回看原实验","View original experiment")}</button>}</div>
        {issue.state!=="resolved"&&revision&&revision.id!==original?.revision_id&&revision.id!==last?.revision_id&&<button type="button" className="button" onClick={()=>void decide(eventBody("proposed",t(`明确关联已保存修订 v${revision.number}。`,`Explicitly link saved revision v${revision.number}.`),revision.id)).catch(()=>{})}>{t(`关联当前已保存版本 v${revision.number}`,`Link selected saved revision v${revision.number}`)}</button>}
        {issue.state!=="resolved"&&run&&run.revision_id===last?.revision_id&&run.id!==last?.target_run_id&&<button type="button" className="button" onClick={()=>void decide(eventBody("proposed",t("明确关联当前已提交实验。","Explicitly link the submitted experiment."),run.revision_id,run.id)).catch(()=>{})}>{t("关联当前已提交实验","Link the selected submitted experiment")}</button>}
        {last?.revision_id&&<p>{t("已关联修订：","Linked revision: ")}v{research.revisions?.find(r=>r.id===last.revision_id)?.number||"?"}{last.target_run_id?` · ${t("实验","Run")} ${shortId(last.target_run_id)}`:t("；请在实验页明确提交此版本。","; explicitly submit this revision in Experiments.")}</p>}
        {issue.state!=="resolved"&&targetRun&&!activeStatuses.has(targetRun.status)&&<>
          <Field label={t("用于关闭问题的接受审核","Accepted review for resolving this issue")}><select value={resolutionId} onChange={e=>setResolutionId(e.target.value)}><option value="">{t("请选择针对当前结果的接受审核","Choose an accepted review for this output")}</option>{accepted.map(review=><option key={review.id} value={review.id}>{formatDate(review.created_at)} · {review.note}</option>)}</select></Field>
          {!accepted.length&&<p>{t("尚无匹配的接受审核。计算完成不代表问题已解决；请先在结果页复审。","No matching accepted review exists. A completed calculation does not resolve the issue; review the result first.")}</p>}
          {last?.disposition==="implementation_fix"&&<Field label={t("实现修复的回归检查","Regression check for the implementation fix")}><select value={checkId} onChange={e=>setCheckId(e.target.value)}><option value="">{t("选择已通过的同条件检查","Select a passed check under the same conditions")}</option>{applicableChecks.map(check=><option key={check.id} value={check.id}>{formatDate(check.created_at)} · {shortId(check.id)}</option>)}</select></Field>}
          <button type="button" className="button primary" disabled={!resolutionId||(last?.disposition==="implementation_fix"&&!checkId)} onClick={()=>void closeIssue()}>{t("依据所选复审关闭问题","Resolve using the selected review")}</button>
        </>}
        {issue.state==="resolved"&&<p role="status">{last?.resolution_review_id?t("问题已按所选复审关闭，历史记录可追溯。","Resolved using the selected review; history is retained."):t("此为历史关闭记录，未关联独立复审。","This legacy closure has no linked independent re-review.")}</p>}
      </>}
    </fieldset>
  </section>;
});
