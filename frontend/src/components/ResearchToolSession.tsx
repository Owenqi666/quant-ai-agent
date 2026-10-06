import {useEffect, useRef, useState} from "react";
import {api} from "../api";
import {describeError} from "../domain";
import type {CaseDetail, ToolCall, ToolSession, ToolSessionPage} from "../generated/api-contract";
import {caseActionNames, caseProposalBody} from "../researchCases";
import {recoveryKey} from "../reviewRecovery";
import {useRecoverableMutation} from "../useRecoverableMutation";
import type {WorkspaceRequestScope} from "../workspaceScope";
import MutationRecovery from "./MutationRecovery";
import {Field, JsonDetails} from "./ui";

const base = "/research-tool-sessions";
export default function ResearchToolSession({detail, workspaceScope}:{detail:CaseDetail; workspaceScope:WorkspaceRequestScope}) {
  const [page,setPage]=useState<ToolSessionPage|null>(null), [selected,setSelected]=useState("");
  const [offset,setOffset]=useState(0), [refresh,setRefresh]=useState(0), [error,setError]=useState(""), [sending,setSending]=useState(false);
  const mounted=useRef(false), busy=useRef(false);
  const mutation=useRecoverableMutation<ToolSession>(recoveryKey(workspaceScope.workspaceId,"research-tool-session-create",detail.id),base);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{
    const captured=workspaceScope.capture(),abort=new AbortController();setPage(null);setError("");
    void api<ToolSessionPage>(`${base}?case_id=${detail.id}&limit=20&offset=${offset}`,{signal:abort.signal}).then(value=>{
      if(workspaceScope.isCurrent(captured,abort.signal)){setPage(value);setSelected(old=>old||value.items[0]?.id||"");}
    }).catch(e=>{if(workspaceScope.isCurrent(captured,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[detail.id,offset,refresh,workspaceScope]);
  async function create(replay=false){
    if(busy.current)return;busy.current=true;setSending(true);setError("");const captured=workspaceScope.capture();
    try{
      const value=await mutation.execute(replay?undefined:{case_id:detail.id,case_digest:detail.digest,budget:{max_calls:8,max_errors:2,max_seconds:30}});
      if(!mounted.current||!workspaceScope.isCurrent(captured))return;
      setSelected(value.id);setOffset(0);setRefresh(v=>v+1);mutation.acknowledge();
    }catch(e){if(mounted.current&&workspaceScope.isCurrent(captured))setError(describeError(e));}
    finally{busy.current=false;if(mounted.current&&workspaceScope.isCurrent(captured))setSending(false);}
  }
  return <details className="panel" aria-label="受限工具会话"><summary>受限工具验证与建议记录（AI 接口留白）</summary>
    <p>每个会话最多 8 次调用、2 次失败、30 秒累计工具执行时间。刷新页面沿用原记录与预算；新会话需要用户主动创建。这里只生成自动化草稿，不保存人工批准。</p>
    {error&&<p role="alert">{error}</p>}
    <MutationRecovery name="工具会话" pending={mutation.pending} error={mutation.error} busy={sending} onRetry={()=>void create(true)} onDismiss={mutation.dismissRejected}/>
    <button className="button" disabled={sending||!!mutation.pending||mutation.blocked||!mutation.ready||!page} onClick={()=>void create()}>{page?.total ? "创建新会话并重新分配预算" : "开始受限工具会话"}</button>
    <div className="button-row"><button className="button small" disabled={!offset} onClick={()=>setOffset(v=>Math.max(0,v-20))}>上一页会话</button><button className="button small" disabled={!page||offset+20>=page.total} onClick={()=>setOffset(v=>v+20)}>下一页会话</button><button className="button small" onClick={()=>setRefresh(v=>v+1)}>刷新工具会话</button></div>
    {!!page?.items.length&&<Field label="选择工具会话"><select value={selected} onChange={e=>setSelected(e.target.value)}>{!page.items.some(item=>item.id===selected)&&selected&&<option value={selected}>当前会话 {selected.slice(-12)}</option>}{page.items.map(item=><option key={item.id} value={item.id}>{item.created_at} · {item.id.slice(-12)}</option>)}</select></Field>}
    {selected&&<SessionDetail key={`${workspaceScope.key}:${selected}`} identity={selected} detail={detail} refresh={refresh} workspaceScope={workspaceScope}/>}
  </details>;
}

function SessionDetail({identity,detail,refresh,workspaceScope}:{identity:string;detail:CaseDetail;refresh:number;workspaceScope:WorkspaceRequestScope}){
  const [session,setSession]=useState<ToolSession|null>(null),[error,setError]=useState(""),[sending,setSending]=useState(false),[nonce,setNonce]=useState(0);
  const [action,setAction]=useState(detail.context.allowed_actions[0]),[rationale,setRationale]=useState(""),[evidenceIds,setEvidenceIds]=useState<string[]>([]);
  const mounted=useRef(false),busy=useRef(false);
  const mutation=useRecoverableMutation<ToolCall>(recoveryKey(workspaceScope.workspaceId,"research-tool-call",identity),`${base}/${identity}/calls`);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{
    const captured=workspaceScope.capture(),abort=new AbortController();setSession(null);setError("");
    void api<ToolSession>(`${base}/${identity}`,{signal:abort.signal}).then(value=>{if(workspaceScope.isCurrent(captured,abort.signal))setSession(value);})
      .catch(e=>{if(workspaceScope.isCurrent(captured,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[identity,refresh,nonce,workspaceScope]);
  async function call(body?:Record<string,unknown>){
    if(busy.current)return;busy.current=true;setSending(true);setError("");const captured=workspaceScope.capture();
    try{
      const saved = await mutation.execute(body);
      if(!mounted.current||!workspaceScope.isCurrent(captured))return;
      mutation.acknowledge(()=>{
        if(saved.response?.ok && saved.response.proposal){setRationale("");setEvidenceIds([]);}
        else if(saved.tool==="propose_next_action"){
          const original=JSON.parse(saved.arguments_json) as {rationale?:string;evidence_ids?:string[]};
          if(typeof original.rationale==="string")setRationale(original.rationale);
          if(Array.isArray(original.evidence_ids))setEvidenceIds(original.evidence_ids);
        }
      });setNonce(v=>v+1);
    }catch(e){if(mounted.current&&workspaceScope.isCurrent(captured))setError(describeError(e));}
    finally{busy.current=false;if(mounted.current&&workspaceScope.isCurrent(captured))setSending(false);}
  }
  function propose(){try{void call(caseProposalBody(detail,action,rationale,evidenceIds));}catch(e){setError(describeError(e));}}
  const locked=sending||!!mutation.pending||mutation.blocked||!mutation.ready||session?.status!=="active";
  return <section aria-label="工具执行与草稿">
    {error&&<p role="alert">{error}</p>}
    <MutationRecovery name="工具调用" pending={mutation.pending} error={mutation.error} busy={sending} onRetry={()=>void call()} onDismiss={mutation.dismissRejected}/>
    {session&&<>
      <p>会话状态：{session.status}；已用调用 {session.usage.calls}/{session.budget.max_calls}，失败 {session.usage.errors}/{session.budget.max_errors}，累计执行 {session.usage.elapsed_seconds.toFixed(3)}/{session.budget.max_seconds} 秒。</p>
      <p className="fine-print">耗时按调用前后检查；超时不发布输出。只读核验不支持强制中断，中断恢复会保守消耗剩余时间预算。</p>
      <fieldset disabled={locked}><div className="button-row">
        <button className="button" onClick={()=>void call({tool:"read_case",arguments:{}})}>核对任务上下文</button>
        <button className="button" onClick={()=>void call({tool:"read_evidence",arguments:{}})}>核对论文与规则依据</button>
        <button className="button" onClick={()=>void call({tool:"read_result",arguments:{}})}>核对实际计算结果</button>
      </div>
      <Field label="建议的下一步"><select value={action} onChange={e=>setAction(e.target.value as typeof action)}>{detail.context.allowed_actions.map(value=><option key={value} value={value}>{caseActionNames[value]}</option>)}</select></Field>
      <Field label="工具建议依据"><textarea maxLength={2000} value={rationale} onChange={e=>setRationale(e.target.value)}/></Field>
      <details><summary>引用已有证据</summary>{detail.context.evidence.map(item=><label key={item.id} style={{display:"block"}}><input type="checkbox" checked={evidenceIds.includes(item.id)} onChange={e=>setEvidenceIds(ids=>e.target.checked?[...ids,item.id]:ids.filter(id=>id!==item.id))}/>{item.id} · {item.locator}</label>)}</details>
      <button className="button" disabled={!rationale.trim()} onClick={propose}>保存待审核工具草稿</button>
      </fieldset>
      <h4>调用记录与输出</h4>{!session.calls.length&&<p>尚无工具调用。</p>}
      {session.calls.map(item=><article key={item.id} aria-label={`工具调用 ${item.sequence}`}><h4>第 {item.sequence} 次 · {item.tool} · {item.status}</h4>
        {item.response?.error&&<p>{item.response.error.code}：{item.response.error.message}；可重试：{item.response.error.retryable?"是":"否"}。</p>}
        {item.response?.proposal&&<div className="info-note"><p><strong>自动化草稿 · 语义尚未审核</strong></p><p>{caseActionNames[item.response.proposal.action]}</p><p>{item.response.proposal.rationale}</p><p>此建议未修改研究结果或保存人工审核。</p></div>}
        <JsonDetails label="查看此调用的请求与实际响应" value={{arguments:JSON.parse(item.arguments_json),response:item.response}}/>
      </article>)}
    </>}
  </section>;
}
