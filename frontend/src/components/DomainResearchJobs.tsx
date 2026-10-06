import {useEffect,useRef,useState} from "react";
import {api} from "../api";
import {describeError,formatDate} from "../domain";
import type {DomainResearchJob,DomainJobPage,DomainJobStep,DomainJobAdvance,MonthlyConfig,MonthlyDefaults,MonthlyStatus,ProtocolPage,StudyPage} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";
import {useRecoverableMutation} from "../useRecoverableMutation";
import {recoveryKey} from "../reviewRecovery";
import {monthlyConfigError} from "../monthlyExperiments";
import MutationRecovery from "./MutationRecovery";
import {Field,JsonDetails} from "./ui";

const names:Record<string,string>={created:"等待领域校验",validated:"来源与配置已校验",submitted:"月度实验已提交",observed:"月度结果已核对",completed:"领域执行完成",blocked:"研究条件阻断",failed:"领域执行失败",exhausted:"领域预算耗尽",cancelled:"领域执行已停止"};
const actions:Record<string,string>={validate:"校验领域来源与配置",submit:"提交受控月度实验",observe:"核对受控月度结果",complete:"完成领域研究执行",cancel:"停止领域研究执行"};
const next:Record<string,DomainJobAdvance["action"]>={created:"validate",validated:"submit",submitted:"observe",observed:"complete"};
type Kind="monthly_fixture"|"author_study_diagnostic";

export default function DomainResearchJobs({workspaceScope,onOpenSource}:{workspaceScope:WorkspaceRequestScope;onOpenSource:(kind:"monthly_experiment"|"author_study",id:string)=>void}){
  const [kind,setKind]=useState<Kind>("monthly_fixture"),[protocols,setProtocols]=useState<ProtocolPage|null>(null),[studies,setStudies]=useState<StudyPage|null>(null),[sourceId,setSourceId]=useState("");
  const [config,setConfig]=useState<MonthlyConfig|null>(null),[sourceOffset,setSourceOffset]=useState(0),[page,setPage]=useState<DomainJobPage|null>(null),[offset,setOffset]=useState(0),[selected,setSelected]=useState(""),[refresh,setRefresh]=useState(0),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const mounted=useRef(false),sending=useRef(false);
  const mutation=useRecoverableMutation<DomainResearchJob>(recoveryKey(workspaceScope.workspaceId,"domain-job-create"),"/domain-research-jobs");
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();
    void api<MonthlyDefaults>("/monthly-experiments/defaults",{signal:abort.signal}).then(v=>{if(workspaceScope.isCurrent(scope,abort.signal))setConfig(v.config);}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[workspaceScope]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setProtocols(null);setStudies(null);setError("");
    const request=kind==="monthly_fixture"?api<ProtocolPage>(`/research-protocols?limit=20&offset=${sourceOffset}`,{signal:abort.signal}):api<StudyPage>(`/author-studies?limit=20&offset=${sourceOffset}`,{signal:abort.signal});
    void request.then(v=>{if(workspaceScope.isCurrent(scope,abort.signal)){if(kind==="monthly_fixture")setProtocols(v as ProtocolPage);else setStudies(v as StudyPage);}}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[kind,sourceOffset,refresh,workspaceScope]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setPage(null);
    void api<DomainJobPage>(`/domain-research-jobs?limit=20&offset=${offset}`,{signal:abort.signal}).then(v=>{if(workspaceScope.isCurrent(scope,abort.signal)){setPage(v);setSelected(old=>old||v.items[0]?.id||"");}}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[offset,refresh,workspaceScope]);
  async function submit(body?:Record<string,unknown>){if(sending.current)return;sending.current=true;setBusy(true);setError("");const scope=workspaceScope.capture();
    try{const value=await mutation.execute(body);if(!mounted.current||!workspaceScope.isCurrent(scope))return;setSelected(value.id);setOffset(0);setRefresh(n=>n+1);mutation.acknowledge();}catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  function create(){const protocol=protocols?.items.find(p=>p.id===sourceId),study=studies?.items.find(s=>s.id===sourceId);
    const source=kind==="monthly_fixture"&&protocol&&config?{kind,protocol_id:protocol.id,protocol_digest:protocol.digest,config}:kind==="author_study_diagnostic"&&study?{kind,study_id:study.id,study_digest:study.digest}:null;
    if(source)void submit({source,budget:{max_steps:12,max_failures:2,max_seconds:300},note:"Explicit user-created provider-free domain execution"});
  }
  const sourcePage=kind==="monthly_fixture"?protocols:studies,locked=busy||!mutation.ready||mutation.blocked||!!mutation.pending;
  return <><section className="panel" aria-label="跨路径研究执行"><h2>月度实验与作者准入的受控执行</h2><p>月度执行使用已有 controlled_fixture 计算器。作者准入只核验诊断并保留停止原因，不生成作者组合收益。</p>
    {error&&<p role="alert">{error}</p>}<MutationRecovery name="领域研究执行" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void submit()} onDismiss={mutation.dismissRejected}/>
    <details><summary>创建一次跨路径研究执行</summary><fieldset disabled={locked}>
      <Field label="领域执行路径"><select value={kind} onChange={e=>{setKind(e.target.value as Kind);setSourceId("");setSourceOffset(0);}}><option value="monthly_fixture">月度项目约定 · 受控夹具</option><option value="author_study_diagnostic">作者准入 · 只读诊断</option></select></Field>
      <Field label="选择领域研究来源"><select value={sourceId} onChange={e=>setSourceId(e.target.value)}><option value="">选择已有准确版本</option>{kind==="monthly_fixture"?protocols?.items.map(p=><option key={p.id} value={p.id}>{p.title} · {p.config.mode}</option>):studies?.items.map(s=><option key={s.id} value={s.id}>{s.title}</option>)}</select></Field>
      <div className="button-row"><button className="button small" disabled={!sourceOffset} onClick={()=>{setSourceId("");setSourceOffset(n=>Math.max(0,n-20));}}>上一页领域来源</button><button className="button small" disabled={!sourcePage||sourceOffset+20>=sourcePage.total} onClick={()=>{setSourceId("");setSourceOffset(n=>n+20);}}>下一页领域来源</button></div>
      {sourcePage?.total===0&&<p>先在「研究规则」保存明确规则，或在「研究准入」导入已有诊断。</p>}
      {kind==="monthly_fixture"&&config&&<><p>以下窗口与阈值是项目实验配置；paper 未决规则不能靠此表单变成已解决。</p>
        <Field label="受控月度开始月份"><input type="month" value={config.start_month} onChange={e=>setConfig({...config,start_month:e.target.value})}/></Field>
        <Field label="受控月度结束月份"><input type="month" value={config.end_month} onChange={e=>setConfig({...config,end_month:e.target.value})}/></Field>
        <Field label="受控月度最低资产数"><input type="number" min={9} max={128} value={config.min_assets} onChange={e=>setConfig({...config,min_assets:Number(e.target.value)})}/></Field>
        <Field label="受控月度成本代理 bps"><input type="number" min={0} max={100} step={0.1} value={config.cost_bps} onChange={e=>setConfig({...config,cost_bps:Number(e.target.value)})}/></Field>
        {monthlyConfigError(config)&&<p role="alert">{monthlyConfigError(config)}</p>}</>}
      <p>最多 12 步、2 次技术失败、创建起算 300 秒；排队和计算包含在内。预算关闭后仅另允许一次安全停止。</p>
      <button className="button primary" disabled={!sourceId||(kind==="monthly_fixture"&&(!config||!!monthlyConfigError(config)))} onClick={create}>创建受控领域执行</button>
    </fieldset></details>
  </section><section className="panel" aria-label="领域执行历史"><h2>保留的领域作业</h2>
    <div className="button-row"><button className="button small" onClick={()=>setRefresh(n=>n+1)}>刷新领域执行</button><button className="button small" disabled={!offset} onClick={()=>setOffset(n=>Math.max(0,n-20))}>上一页领域作业</button><button className="button small" disabled={!page||offset+20>=page.total} onClick={()=>setOffset(n=>n+20)}>下一页领域作业</button></div><p>共 {page?.total??"…"} 项</p>
    {page?.items.map(j=><p key={j.id}><button className="text-button" aria-label={`查看领域研究执行 ${j.id}`} onClick={()=>setSelected(j.id)}>{formatDate(j.created_at)} · {names[j.state]} · {j.source_kind}</button></p>)}
  </section>{selected&&<DomainDetail key={`${workspaceScope.key}:${selected}`} id={selected} refresh={refresh} workspaceScope={workspaceScope} onOpenSource={onOpenSource}/>}</>;
}

function DomainDetail({id,refresh,workspaceScope,onOpenSource}:{id:string;refresh:number;workspaceScope:WorkspaceRequestScope;onOpenSource:(kind:"monthly_experiment"|"author_study",id:string)=>void}){
  const [job,setJob]=useState<DomainResearchJob|null>(null),[runStatus,setRunStatus]=useState(""),[error,setError]=useState(""),[busy,setBusy]=useState(false),[nonce,setNonce]=useState(0);
  const mounted=useRef(false),sending=useRef(false);const mutation=useRecoverableMutation<DomainJobStep>(recoveryKey(workspaceScope.workspaceId,"domain-job-advance",id),`/domain-research-jobs/${id}/advance`);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setJob(null);setError("");void api<DomainResearchJob>(`/domain-research-jobs/${id}`,{signal:abort.signal}).then(v=>{if(workspaceScope.isCurrent(scope,abort.signal))setJob(v);}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();},[id,nonce,refresh,workspaceScope]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined;setRunStatus("");async function poll(){if(!job?.experiment_id)return;try{const s=await api<MonthlyStatus>(`/monthly-experiments/${job.experiment_id}/status`,{signal:abort.signal});if(workspaceScope.isCurrent(scope,abort.signal)){setRunStatus(s.status);if(["queued","running","cancelling"].includes(s.status))timer=setTimeout(()=>void poll(),1000);}}catch(e){if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));}}void poll();return()=>{abort.abort();if(timer)clearTimeout(timer);};},[job?.experiment_id,job?.state,workspaceScope]);
  async function advance(action?:DomainJobAdvance["action"]){if(sending.current)return;sending.current=true;setBusy(true);setError("");const scope=workspaceScope.capture();try{await mutation.execute(action?{action}:undefined);if(mounted.current&&workspaceScope.isCurrent(scope)){mutation.acknowledge();setNonce(n=>n+1);}}catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}}
  if(!job)return <section className="panel" role={error?"alert":"status"}>{error||"正在核验领域作业"}</section>;
  const action=next[job.state],waiting=job.state==="submitted"&&(!runStatus||["queued","running","cancelling"].includes(runStatus)),canStop=!!action||(job.state==="exhausted"&&["queued","running","cancelling"].includes(runStatus));
  return <section className="panel" aria-label="领域研究执行详情" data-domain-job-id={job.id}><h2>{names[job.state]}</h2><p>{job.stop_reason||"固定来源和原预算逐步推进；语义与研究价值仍待人工判断。"}</p><p>已用 {Math.min(job.usage.steps,job.budget.max_steps)}/{job.budget.max_steps} 步{job.usage.steps>job.budget.max_steps?"，另有安全停止记录":""}，失败 {job.usage.failures}/{job.budget.max_failures} 次，时间 {job.usage.elapsed_seconds.toFixed(2)}/{job.budget.max_seconds} 秒。</p>
    {error&&<p role="alert">{error}</p>}<MutationRecovery name="领域执行步骤" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void advance()} onDismiss={mutation.dismissRejected}/>
    <div className="button-row">{action&&<button className="button primary" disabled={busy||waiting||!mutation.ready||mutation.blocked||!!mutation.pending} onClick={()=>void advance(action)}>{actions[action]}</button>}<button className="button" disabled={busy} onClick={()=>setNonce(n=>n+1)}>重新核验领域作业</button>{canStop&&<button className="button" disabled={busy||!mutation.ready||mutation.blocked||!!mutation.pending} onClick={()=>void advance("cancel")}>停止领域研究执行</button>}
    {job.experiment_id&&<button className="button" onClick={()=>onOpenSource("monthly_experiment",job.experiment_id!)}>查看本次月度实际输出</button>}{job.source_kind==="author_study_diagnostic"&&<button className="button" onClick={()=>onOpenSource("author_study",job.source_id)}>查看作者准入诊断</button>}{["completed","blocked","failed","cancelled","exhausted"].includes(job.state)&&<a href={`/api/domain-research-jobs/${job.id}/markdown`} download>下载领域执行记录</a>}</div>
    {waiting&&<p role="status">月度状态 {runStatus||"正在读取"}；轮询不新增执行步骤。</p>}{job.result_json&&<JsonDetails label="查看领域冻结结果" value={JSON.parse(job.result_json)}/>}<JsonDetails label="领域作业的冻结上下文与步骤" value={{source:job.source,context:JSON.parse(job.context_json),steps:job.steps}}/>
  </section>;
}
