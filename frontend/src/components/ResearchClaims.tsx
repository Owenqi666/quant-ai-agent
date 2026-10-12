import {useEffect,useRef,useState} from "react";
import {api,post} from "../api";
import {describeError} from "../domain";
import type {CaseDetail,ClaimDetail,ClaimPage,ClaimPreview} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";
import {useRecoverableMutation} from "../useRecoverableMutation";
import {recoveryKey} from "../reviewRecovery";
import MutationRecovery from "./MutationRecovery";
import {Field,JsonDetails} from "./ui";
import ClaimReviews from "./ClaimReviews";
import {defaultMetricPointer} from "../researchCases";

export default function ResearchClaims({detail,workspaceScope}:{detail:CaseDetail;workspaceScope:WorkspaceRequestScope}) {
  const [resultId,setResultId]=useState(detail.context.results[0]?.id||"");
  const [pointer,setPointer]=useState(defaultMetricPointer(detail.context.results[0])),[text,setText]=useState("");
  const [preview,setPreview]=useState<ClaimPreview|null>(null),[previewBody,setPreviewBody]=useState<Record<string,unknown>|null>(null);
  const [page,setPage]=useState<ClaimPage|null>(null),[offset,setOffset]=useState(0),[refresh,setRefresh]=useState(0);
  const [saved,setSaved]=useState<ClaimDetail|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const mounted=useRef(false),sending=useRef(false),epoch=useRef(0);
  const mutation=useRecoverableMutation<ClaimDetail>(recoveryKey(workspaceScope.workspaceId,"research-claims",detail.id),"/research-claims");
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;epoch.current++;};},[]);
  useEffect(()=>{
    const capture=workspaceScope.capture(),abort=new AbortController();setPage(null);
    void api<ClaimPage>(`/research-claims?case_id=${detail.id}&limit=20&offset=${offset}`,{signal:abort.signal}).then(v=>{
      if(workspaceScope.isCurrent(capture,abort.signal))setPage(v);
    }).catch(e=>{if(workspaceScope.isCurrent(capture,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[detail.id,offset,refresh,workspaceScope]);
  function invalidate(){epoch.current++;setPreview(null);setPreviewBody(null);}
  async function check(){if(sending.current)return;const result=detail.context.results.find(r=>r.id===resultId);if(!result)return;
    sending.current=true;setBusy(true);setError("");const capture=workspaceScope.capture(),sequence=++epoch.current;
    const body={case_id:detail.id,case_digest:detail.digest,claims:[{id:"metric-reference",kind:"metric",attribution:"project_convention",text:text.trim(),evidence_ids:[],
      metric_references:[{case_id:detail.id,case_digest:detail.digest,result_id:result.id,result_digest:result.digest,pointer}]}]};
    try{const v=await post<ClaimPreview>("/research-claims/preview",body);if(mounted.current&&workspaceScope.isCurrent(capture)&&sequence===epoch.current){setPreview(v);setPreviewBody(body);}}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(capture)&&sequence===epoch.current)setError(describeError(e));}
    finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(capture))setBusy(false);}
  }
  async function save(body?:Record<string,unknown>){if(sending.current)return;sending.current=true;setBusy(true);setError("");const capture=workspaceScope.capture();
    try{const v=await mutation.execute(body);if(!mounted.current||!workspaceScope.isCurrent(capture))return;setSaved(v);setOffset(0);setRefresh(n=>n+1);mutation.acknowledge(()=>{setText("");invalidate();});}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(capture))setError(describeError(e));}
    finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(capture))setBusy(false);}
  }
  async function open(id:string){const capture=workspaceScope.capture(),sequence=++epoch.current;setSaved(null);setError("");
    try{const v=await api<ClaimDetail>(`/research-claims/${id}`);if(mounted.current&&workspaceScope.isCurrent(capture)&&sequence===epoch.current)setSaved(v);}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(capture)&&sequence===epoch.current)setError(describeError(e));}
  }
  const locked=busy||!!mutation.pending||mutation.blocked||!mutation.ready;
  return <details><summary>带实际数值引用的结论草稿</summary>
    <p>服务核对结果身份并解析指标数值；文字解释始终待人工审核。当前入口保存项目结果引用，不代表论文原始结论或人工批准。</p>
    {error&&<p role="alert">{error}</p>}
    <MutationRecovery name="结论草稿" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void save()} onDismiss={mutation.dismissRejected}/>
    <fieldset disabled={locked}>
      <Field label="引用已冻结的结果"><select value={resultId} onChange={e=>{setResultId(e.target.value);setPointer(defaultMetricPointer(detail.context.results.find(r=>r.id===e.target.value)));invalidate();}}>{detail.context.results.map(r=><option key={r.id} value={r.id}>{r.summary}</option>)}</select></Field>
      <Field label="指标 JSON pointer"><input value={pointer} maxLength={500} onChange={e=>{setPointer(e.target.value);invalidate();}}/></Field>
      <p className="fine-print">指标路径只引用本次保存的工具输出；行业 MOM 使用实际策略位置的 gross 指标，不使用日度 IC 或成本代理。仅接受此类结果允许的有限数值字段。</p>
      <Field label="待审核的解释文字"><textarea value={text} maxLength={4000} onChange={e=>{setText(e.target.value);invalidate();}}/></Field>
      <div className="button-row"><button className="button" disabled={!text.trim()||!pointer.trim()} onClick={()=>void check()}>核对实际指标引用</button><button className="button" disabled={!previewBody} onClick={()=>{if(previewBody)void save(previewBody);}}>保存已核对的结论草稿</button></div>
    </fieldset>
    {preview&&<ClaimView value={preview}/>}
    <div className="button-row"><button className="button small" disabled={!offset} onClick={()=>setOffset(n=>Math.max(0,n-20))}>上一页结论</button><button className="button small" disabled={!page||offset+20>=page.total} onClick={()=>setOffset(n=>n+20)}>下一页结论</button></div>
    {page?.items.map(c=><p key={c.id}><button className="text-button" disabled={locked} onClick={()=>void open(c.id)}>查看结论草稿 {c.created_at}</button></p>)}
    {saved&&<><ClaimView value={saved}/><a className="button" href={`/api/research-claims/${saved.id}/markdown`} download>下载结论引用记录</a><ClaimReviews key={saved.id} claims={saved} workspaceScope={workspaceScope}/></>}
  </details>;
}
function ClaimView({value}:{value:ClaimPreview}){
  return <section aria-label="结论引用核验">{value.claims.map(c=><article key={c.id}><p><strong>服务计算的实际值：</strong>{c.authoritative_display||"此项没有数值引用"}</p><p><strong>待人工审核的文字：</strong>{c.narrative_text}</p><p>语义状态：未审核；引用完整性：{c.citation_integrity}。</p></article>)}<JsonDetails label="查看精确结果引用与限制" value={value}/></section>;
}
