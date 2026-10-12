import {useEffect,useRef,useState} from "react";
import {api,post} from "../api";
import {describeError} from "../domain";
import type {SemanticMaterialCollection,SemanticEvaluationSetPreview,SemanticEvaluationSetDetail,SemanticEvaluationSetPage,SemanticEvaluationComparisonDetail,SemanticEvaluationComparisonPage} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";
import {useRecoverableMutation} from "../useRecoverableMutation";
import {recoveryKey} from "../reviewRecovery";
import MutationRecovery from "./MutationRecovery";
import {Field,JsonDetails} from "./ui";

export default function SemanticEvaluationSets({materials,workspaceScope}:{materials:SemanticMaterialCollection;workspaceScope:WorkspaceRequestScope}){
  const [cases,setCases]=useState(materials.items.map(m=>m.case_id)),[preview,setPreview]=useState<SemanticEvaluationSetPreview|null>(null),[body,setBody]=useState<Record<string,unknown>|null>(null);
  const [page,setPage]=useState<SemanticEvaluationSetPage|null>(null),[selected,setSelected]=useState(""),[offset,setOffset]=useState(0),[refresh,setRefresh]=useState(0),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const mounted=useRef(false),sending=useRef(false),epoch=useRef(0);
  const mutation=useRecoverableMutation<SemanticEvaluationSetDetail>(recoveryKey(workspaceScope.workspaceId,"semantic-reference-freeze",materials.material_sha256),"/semantic-evaluation-sets");
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;epoch.current++;};},[]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setPage(null);
    void api<SemanticEvaluationSetPage>(`/semantic-evaluation-sets?limit=20&offset=${offset}`,{signal:abort.signal}).then(v=>{if(workspaceScope.isCurrent(scope,abort.signal))setPage(v);}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[workspaceScope,offset,refresh]);
  function invalidate(){epoch.current++;setPreview(null);setBody(null);}
  async function check(){if(sending.current)return;sending.current=true;setBusy(true);setError("");setPreview(null);setBody(null);const scope=workspaceScope.capture(),seq=++epoch.current;
    try{const v=await post<SemanticEvaluationSetPreview>("/semantic-evaluation-sets/preview",{material_sha256:materials.material_sha256,case_ids:cases});if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current){setPreview(v);setBody({material_sha256:v.material_sha256,case_ids:v.case_ids,expected_active_annotations:v.active_human_annotations});}}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current)setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  async function save(request?:Record<string,unknown>){if(sending.current)return;sending.current=true;setBusy(true);setError("");const scope=workspaceScope.capture();
    try{const v=await mutation.execute(request);if(mounted.current&&workspaceScope.isCurrent(scope)){setSelected(v.id);setOffset(0);setRefresh(n=>n+1);mutation.acknowledge(invalidate);}}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  const locked=busy||!mutation.ready||!!mutation.pending||mutation.blocked;
  return <section className="panel semantic-material" aria-label="版本化评测参考集"><h2>冻结本次材料参考版本</h2>
    <p>保存选中开发材料及全部活动人工判断的准确快照。未填写、未评定和冲突会保留；自动化不会成为人工参考。后续标注变化需创建新版本。</p>
    {error&&<p role="alert">{error}</p>}<MutationRecovery name="参考集冻结" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void save()} onDismiss={mutation.dismissRejected}/>
    <details><summary>选择材料并冻结参考集</summary><fieldset disabled={locked}>
      {materials.items.map(m=><Field key={m.case_id} label={`冻结材料 ${m.case_id}`}><input type="checkbox" checked={cases.includes(m.case_id)} onChange={e=>{setCases(v=>e.target.checked?[...v,m.case_id]:v.filter(id=>id!==m.case_id));invalidate();}}/></Field>)}
      <div className="button-row"><button className="button" disabled={!cases.length} onClick={()=>void check()}>预览本次参考快照</button><button className="button primary" disabled={!body} onClick={()=>{if(body)void save(body);}}>冻结本次参考版本</button></div>
    </fieldset></details>
    {preview&&<><ReferenceSummary value={preview}/><JsonDetails label="待冻结的材料与全部判断" value={preview}/></>}
    <div className="button-row"><button className="button small" onClick={()=>setRefresh(n=>n+1)}>刷新参考集历史</button><button className="button small" disabled={!offset} onClick={()=>setOffset(v=>Math.max(0,v-20))}>上一页参考集</button><button className="button small" disabled={!page||offset+20>=page.total} onClick={()=>setOffset(v=>v+20)}>下一页参考集</button></div>
    {page?.items.map(s=><p key={s.id}><button className="text-button" disabled={locked} onClick={()=>setSelected(s.id)}>查看参考版本 {s.created_at} · {s.case_ids.length} 材料</button></p>)}
    {selected&&<SetDetail key={`${workspaceScope.key}:${selected}`} id={selected} workspaceScope={workspaceScope}/>}
  </section>;
}

function ReferenceSummary({value}:{value:SemanticEvaluationSetPreview}){const s=value.summary;return <p>共 {s.total_dimensions} 个维度：可比较 {s.eligible_dimensions}，待人工填写 {s.pending_dimensions}，未评定 {s.unknown_dimensions}，冲突 {s.conflicting_dimensions}。语义质量尚未测量；这些仍是开发材料。</p>;}

function SetDetail({id,workspaceScope}:{id:string;workspaceScope:WorkspaceRequestScope}){
  const [detail,setDetail]=useState<SemanticEvaluationSetDetail|null>(null),[history,setHistory]=useState<SemanticEvaluationComparisonPage|null>(null),[requestBody,setRequestBody]=useState<Record<string,unknown>|null>(null);
  const [saved,setSaved]=useState<SemanticEvaluationComparisonDetail|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false),[refresh,setRefresh]=useState(0),[offset,setOffset]=useState(0);
  const mounted=useRef(false),epoch=useRef(0),sending=useRef(false);
  const mutation=useRecoverableMutation<SemanticEvaluationComparisonDetail>(recoveryKey(workspaceScope.workspaceId,"semantic-reference-compare",id),"/semantic-evaluation-comparisons");
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;epoch.current++;};},[]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setDetail(null);setHistory(null);
    void Promise.all([api<SemanticEvaluationSetDetail>(`/semantic-evaluation-sets/${id}`,{signal:abort.signal}),api<SemanticEvaluationComparisonPage>(`/semantic-evaluation-sets/${id}/comparisons?limit=20&offset=${offset}`,{signal:abort.signal})]).then(([s,h])=>{if(workspaceScope.isCurrent(scope,abort.signal)){setDetail(s);setHistory(h);}}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[id,workspaceScope,refresh,offset]);
  async function read(file:File|null){const scope=workspaceScope.capture(),seq=++epoch.current;setRequestBody(null);setError("");if(!file)return;
    try{if(file.size>256*1024)throw new Error("核对 JSON 最大 256 KiB。");const value:unknown=JSON.parse(await file.text());if(!value||typeof value!=="object"||Array.isArray(value))throw new Error("请选择结构化核对请求 JSON。");const v=value as Record<string,unknown>;
      if(v.set_id!==id||v.set_digest!==detail?.digest||"idempotency_key" in v)throw new Error("核对请求必须绑定当前参考版本，且不能预填提交键。");
      if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current)setRequestBody(v);
    }catch(e){if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current)setError(describeError(e));}
  }
  async function save(body?:Record<string,unknown>){if(sending.current)return;sending.current=true;setBusy(true);const scope=workspaceScope.capture();setError("");
    try{const v=await mutation.execute(body);if(mounted.current&&workspaceScope.isCurrent(scope)){setSaved(v);setOffset(0);setRefresh(n=>n+1);mutation.acknowledge(()=>setRequestBody(null));}}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  return <section aria-label="评测参考版本详情" data-set-id={id}>{error&&<p role="alert">{error}</p>}{detail&&<>
    <h3>参考版本 {detail.created_at}</h3><ReferenceSummary value={detail}/><JsonDetails label="冻结参考与来源的准确身份" value={detail}/>
    <a className="button" href={`/api/semantic-evaluation-sets/${id}/export`} download>下载参考集与核对记录</a>
    <details><summary>导入结构化结果核对</summary><p>使用导出记录中的 set_id/digest，填写全部案例五维声明、来源、审核者、实际时间及执行引用。声明一致性不代表模型质量或执行身份认证。请求格式见操作说明/API 文档。</p>
      <MutationRecovery name="参考结果核对" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void save()} onDismiss={mutation.dismissRejected}/>
      <fieldset disabled={busy||!mutation.ready||!!mutation.pending||mutation.blocked}><Field label="选择当前版本核对 JSON"><input type="file" accept="application/json,.json" onChange={e=>void read(e.target.files?.[0]??null)}/></Field>
        {requestBody&&<JsonDetails label="本次完整核对声明" value={requestBody}/>}<button className="button" disabled={!requestBody} onClick={()=>{if(requestBody)void save(requestBody);}}>保存结构化参考核对</button>
      </fieldset>
    </details>
  </>}
    {saved&&<p role="status">核对已保存：匹配 {saved.summary.matched_dimensions} / 可比较 {saved.summary.comparable_dimensions} / 总维度 {saved.summary.total_dimensions}；一致性率 {saved.summary.declaration_agreement_rate??"未测量"}。模型质量未测量。</p>}
    <details><summary>参考结果核对历史</summary><div className="button-row"><button className="button small" disabled={!offset} onClick={()=>setOffset(v=>Math.max(0,v-20))}>上一页核对</button><button className="button small" disabled={!history||offset+20>=history.total} onClick={()=>setOffset(v=>v+20)}>下一页核对</button></div>{history?.items.map(c=><JsonDetails key={c.id} label={`核对 ${c.created_at}`} value={c}/>)}</details>
  </section>;
}
