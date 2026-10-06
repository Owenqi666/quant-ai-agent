import {useEffect,useRef,useState} from "react";
import {api} from "../api";
import {describeError,formatDate} from "../domain";
import type {Research} from "../domain";
import type {ResearchJob,ResearchJobPage,JobStep,GuardStatus,CaseDetail,CasePreview} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";
import {useRecoverableMutation} from "../useRecoverableMutation";
import {recoveryKey} from "../reviewRecovery";
import MutationRecovery from "./MutationRecovery";
import {Field,JsonDetails} from "./ui";

const states:Record<string,string>={created:"等待校验草稿",validated:"草稿已校验",committed:"研究修订已保存",submitted:"实验已提交",observed:"结果已核对",completed:"研究执行完成",blocked:"研究被阻断",failed:"执行失败",cancelled:"已停止",exhausted:"预算耗尽"};
const actions:Record<string,string>={validate:"校验候选草稿",commit:"保存已校验的研究修订",submit:"提交冻结实验",observe:"核对本次实验",complete:"完成研究执行",cancel:"停止研究执行"};
const next:Record<string,string>={created:"validate",validated:"commit",committed:"submit",submitted:"observe",observed:"complete"};
export default function ResearchJobs({workspaceScope,onOpenRun,onOpenCases}:{workspaceScope:WorkspaceRequestScope;onOpenRun:(id:string)=>void;onOpenCases:()=>void}) {
  const [catalog,setCatalog]=useState<{id:string;title:string}[]>([]),[cursors,setCursors]=useState([0]),[nextCursor,setNextCursor]=useState<number|null>(null);
  const [researchId,setResearchId]=useState(""),[research,setResearch]=useState<Research|null>(null),[chosen,setChosen]=useState<string[]>([]);
  const [guard,setGuard]=useState<GuardStatus|null>(null),[page,setPage]=useState<ResearchJobPage|null>(null),[offset,setOffset]=useState(0);
  const [selected,setSelected]=useState(""),[refresh,setRefresh]=useState(0),[error,setError]=useState(""),[busy,setBusy]=useState(false),[partial,setPartial]=useState(false);
  const cursor=cursors[cursors.length-1];
  const mounted=useRef(false),sending=useRef(false);
  const mutation=useRecoverableMutation<ResearchJob>(recoveryKey(workspaceScope.workspaceId,"research-job-create"),"/research-jobs");
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{
    const capture=workspaceScope.capture(),abort=new AbortController();setCatalog([]);
    void api<{items:{id:string;title:string}[];has_more:boolean;next_cursor:number}>(`/catalog/researches?limit=20&after=${cursor}`,{signal:abort.signal})
      .then(v=>{if(workspaceScope.isCurrent(capture,abort.signal)){setCatalog(v.items);setNextCursor(v.has_more?v.next_cursor:null);}})
      .catch(e=>{if(workspaceScope.isCurrent(capture,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[workspaceScope,cursor,refresh]);
  useEffect(()=>{
    const capture=workspaceScope.capture(),abort=new AbortController();setResearch(null);setGuard(null);setChosen([]);
    if(researchId)void Promise.all([api<Research>(`/researches/${researchId}`,{signal:abort.signal}),api<GuardStatus>(`/researches/${researchId}/temporal-guard`,{signal:abort.signal})])
      .then(([r,g])=>{if(workspaceScope.isCurrent(capture,abort.signal)){setResearch(r);setGuard(g);}})
      .catch(e=>{if(workspaceScope.isCurrent(capture,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[researchId,workspaceScope]);
  useEffect(()=>{
    const capture=workspaceScope.capture(),abort=new AbortController();setPage(null);
    void api<ResearchJobPage>(`/research-jobs?limit=20&offset=${offset}`,{signal:abort.signal}).then(v=>{
      if(workspaceScope.isCurrent(capture,abort.signal)){setPage(v);setSelected(old=>old||v.items[0]?.id||"");}
    }).catch(e=>{if(workspaceScope.isCurrent(capture,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[offset,refresh,workspaceScope]);
  async function submit(body?:Record<string,unknown>){
    if(sending.current)return;sending.current=true;setBusy(true);setError("");const capture=workspaceScope.capture();
    try{const v=await mutation.execute(body);if(!mounted.current||!workspaceScope.isCurrent(capture))return;
      setSelected(v.id);setOffset(0);setRefresh(n=>n+1);mutation.acknowledge(()=>setChosen([]));
    }catch(e){if(mounted.current&&workspaceScope.isCurrent(capture))setError(describeError(e));}
    finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(capture))setBusy(false);}
  }
  function create(){
    const base=research?.revisions?.find(r=>r.id===research.latest_revision_id);if(!base||!chosen.length)return;
    const candidates=base.task.candidates.filter(c=>chosen.includes(c.id));
    const hypotheses=base.task.hypotheses.filter(h=>candidates.some(c=>c.hypothesis_id===h.id));
    const evidence=base.task.evidence.filter(e=>hypotheses.some(h=>h.evidence_ids.includes(e.id)));
    void submit({research_id:research!.id,base_revision_id:base.id,draft:{evidence,hypotheses,candidates},budget:{max_steps:12,max_failures:2,max_seconds:300},
      note:"Explicit user-created provider-free research execution",allow_partial_execution:partial,method_status:"resolved",unresolved_rules:[]});
  }
  const locked=busy||!mutation.ready||mutation.blocked||!!mutation.pending;
  return <>
    <section className="panel" aria-label="研究执行入口"><h2>候选草稿 → 校验 → 修订 → 实验 → 结果</h2>
      <p>选择已有论文和数据下的候选，显式创建一次受控执行。AI 接口尚未启用；数据、评估区间和原计算预算沿用所选研究。</p>
      {error&&<p role="alert">{error}</p>}
      <MutationRecovery name="研究执行" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void submit()} onDismiss={mutation.dismissRejected}/>
      <details><summary>创建一次研究执行</summary><fieldset disabled={locked}>
        <Field label="选择执行研究"><select value={researchId} onChange={e=>setResearchId(e.target.value)}><option value="">选择一项研究</option>{catalog.map(r=><option key={r.id} value={r.id}>{r.title}</option>)}</select></Field>
        <div className="button-row"><button className="button small" disabled={cursors.length<=1} onClick={()=>setCursors(v=>v.slice(0,-1))}>上一页研究</button><button className="button small" disabled={nextCursor===null} onClick={()=>{if(nextCursor!==null)setCursors(v=>[...v,nextCursor]);}}>下一页研究</button></div>
        {research&&<><p>当前版本：{research.revisions?.find(r=>r.id===research.latest_revision_id)?.number}。请选择本次要执行的候选。</p>
          {research.revisions?.find(r=>r.id===research.latest_revision_id)?.task.candidates.map(c=><p key={c.id}><label><input type="checkbox" checked={chosen.includes(c.id)} onChange={e=>setChosen(v=>e.target.checked?[...v,c.id]:v.filter(id=>id!==c.id))}/> 执行候选 {c.id}</label> · {c.expression}</p>)}
          <label><input type="checkbox" checked={partial} onChange={e=>setPartial(e.target.checked)}/> 明确允许部分候选执行，保留其余候选的阻断诊断</label>
        </>}
        <p>本次最多 12 个研究步骤、2 次技术失败；预算闭合后另允许 1 条安全停止记录。从创建开始最多 300 秒，包含排队和计算等待。刷新不会重置预算。</p>
        {guard&&<JsonDetails label="查看数据保留区间与历史暴露" value={guard}/>}
        <button className="button primary" disabled={!research||!chosen.length} onClick={create}>创建受控研究执行</button>
      </fieldset></details>
    </section>
    <section className="panel" aria-label="研究执行历史"><h2>已保存的执行任务</h2>
      <div className="button-row"><button className="button small" onClick={()=>setRefresh(n=>n+1)}>刷新执行任务</button><button className="button small" disabled={!offset} onClick={()=>setOffset(n=>Math.max(0,n-20))}>上一页任务</button><button className="button small" disabled={!page||offset+20>=page.total} onClick={()=>setOffset(n=>n+20)}>下一页任务</button></div>
      <p>{page?`本页 ${page.items.length} / 共 ${page.total} 项`:"正在读取执行任务"}</p>
      {page?.items.map(j=><p key={j.id}><button className="text-button" aria-label={`查看研究执行 ${j.id}`} onClick={()=>setSelected(j.id)}>{formatDate(j.created_at)} · {states[j.state]}</button></p>)}
    </section>
    {selected&&<JobDetail key={`${workspaceScope.key}:${selected}`} id={selected} refresh={refresh} workspaceScope={workspaceScope} onOpenRun={onOpenRun} onOpenCases={onOpenCases}/>}
  </>;
}
function JobDetail({id,refresh,workspaceScope,onOpenRun,onOpenCases}:{id:string;refresh:number;workspaceScope:WorkspaceRequestScope;onOpenRun:(id:string)=>void;onOpenCases:()=>void}){
  const [job,setJob]=useState<ResearchJob|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false),[nonce,setNonce]=useState(0);
  const mounted=useRef(false),sending=useRef(false),[runStatus,setRunStatus]=useState("");
  const mutation=useRecoverableMutation<JobStep>(recoveryKey(workspaceScope.workspaceId,"research-job-advance",id),`/research-jobs/${id}/advance`);
  const link=useRecoverableMutation<CaseDetail>(recoveryKey(workspaceScope.workspaceId,"research-job-case",id),"/research-cases");
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{
    const capture=workspaceScope.capture(),abort=new AbortController();setJob(null);setError("");
    void api<ResearchJob>(`/research-jobs/${id}`,{signal:abort.signal}).then(v=>{if(workspaceScope.isCurrent(capture,abort.signal))setJob(v);})
      .catch(e=>{if(workspaceScope.isCurrent(capture,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[id,refresh,nonce,workspaceScope]);
  useEffect(()=>{
    const capture=workspaceScope.capture(),abort=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined;setRunStatus("");
    async function poll(){if(!job?.run_id)return;try{const v=await api<{status:string}>(`/runs/${job.run_id}/status`,{signal:abort.signal});
      if(!workspaceScope.isCurrent(capture,abort.signal))return;setRunStatus(v.status);
      if(["queued","running","cancelling"].includes(v.status))timer=setTimeout(()=>void poll(),1000);
    }catch(e){if(workspaceScope.isCurrent(capture,abort.signal))setError(describeError(e));}}
    if(job?.run_id)void poll();return()=>{abort.abort();if(timer)clearTimeout(timer);};
  },[job?.run_id,job?.state,workspaceScope]);
  async function advance(action?:string){if(sending.current)return;sending.current=true;setBusy(true);setError("");const capture=workspaceScope.capture();
    try{await mutation.execute(action?{action}:undefined);if(!mounted.current||!workspaceScope.isCurrent(capture))return;mutation.acknowledge();setNonce(n=>n+1);}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(capture))setError(describeError(e));}
    finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(capture))setBusy(false);}
  }
  async function bind(){if(sending.current||!job?.run_id)return;sending.current=true;setBusy(true);setError("");const capture=workspaceScope.capture();
    try{let body:Record<string,unknown>|undefined;
      if(!link.pending){const preview=await api<CasePreview>(`/research-cases/preview?source_kind=daily_run&source_id=${job.run_id}`);body={title:"受控研究执行结果",note:"Existing verified experiment linked; semantic review pending.",source_kind:"daily_run",source_id:job.run_id,source_digest:preview.source_digest};}
      if(!mounted.current||!workspaceScope.isCurrent(capture))return;
      await link.execute(body);if(!mounted.current||!workspaceScope.isCurrent(capture))return;link.acknowledge();onOpenCases();
    }catch(e){if(mounted.current&&workspaceScope.isCurrent(capture))setError(describeError(e));}
    finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(capture))setBusy(false);}
  }
  if(!job)return <section className="panel" role={error?"alert":"status"}>{error||"正在核验研究执行记录"}</section>;
  const action=next[job.state],canStop=!!action||(job.state==="exhausted"&&!!job.run_id&&["queued","running","cancelling"].includes(runStatus)),waiting=job.state==="submitted"&&(!runStatus||["queued","running","cancelling"].includes(runStatus));
  return <section className="panel" aria-label="研究执行详情" data-job-id={id}><h2>{states[job.state]}</h2>
    <p>{job.stop_reason||"按照固定论文、数据、开发区间和原预算逐步推进。语义仍需人工审核。"}</p>
    <p>已用研究步骤 {Math.min(job.usage.steps,job.budget.max_steps)}/{job.budget.max_steps}{job.usage.steps>job.budget.max_steps?"，另有 1 条安全停止记录":""}；技术失败 {job.usage.failures}/{job.budget.max_failures}；全流程时间 {job.usage.elapsed_seconds.toFixed(2)}/{job.budget.max_seconds} 秒。</p>
    {error&&<p role="alert">{error}</p>}
    <MutationRecovery name="执行步骤" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void advance()} onDismiss={mutation.dismissRejected}/>
    <MutationRecovery name="执行结果关联" pending={link.pending} error={link.error} busy={busy} onRetry={()=>void bind()} onDismiss={link.dismissRejected}/>
    <div className="button-row">
      {action&&<button className="button primary" disabled={busy||waiting||!!mutation.pending||mutation.blocked||!mutation.ready} onClick={()=>void advance(action)}>{actions[action]}</button>}
      <button className="button" disabled={busy} onClick={()=>setNonce(n=>n+1)}>重新核验执行记录</button>
      {canStop&&<button className="button" disabled={busy||!!mutation.pending||mutation.blocked||!mutation.ready} onClick={()=>void advance("cancel")}>停止研究执行</button>}
      {job.run_id&&<button className="button" onClick={()=>onOpenRun(job.run_id!)}>打开本次实际输出与审核</button>}
      {job.state==="completed"&&job.run_id&&<button className="button" disabled={busy||!!link.pending||link.blocked||!link.ready} onClick={()=>void bind()}>关联到研究任务</button>}
      {["completed","blocked","failed","cancelled","exhausted"].includes(job.state)&&<a className="button" href={`/api/research-jobs/${id}/markdown`} download>下载完整执行记录</a>}
    </div>
    {waiting&&<p role="status">实验状态：{runStatus||"正在读取"}。结果完成后才启用核对步骤；状态轮询不新增执行步骤。</p>}
    {!!job.candidate_checks.length&&<JsonDetails label="候选校验与归因记录" value={job.candidate_checks}/>}
    {job.result_json&&<JsonDetails label="查看保存的确定性结果" value={JSON.parse(job.result_json)}/>}
    <details><summary>完整步骤与冻结上下文</summary><JsonDetails label="冻结上下文" value={JSON.parse(job.context_json)}/>{job.steps.map(s=><article key={s.id}><h3>第 {s.sequence} 步：{actions[s.action]} · {s.status}</h3><p>{s.error?.message}</p><JsonDetails label="步骤实际响应及效果身份" value={s}/></article>)}</details>
  </section>;
}
