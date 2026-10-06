import {useEffect,useRef,useState} from "react";
import {api,post} from "../api";
import {describeError,formatDate} from "../domain";
import {assessmentDimensions} from "../researchAssessment";
import type {Dimension} from "../researchAssessment";
import {emptySemanticDraft,readSemanticDraft,saveSemanticDraft} from "../semanticAnnotationDraft";
import type {SemanticDraft} from "../semanticAnnotationDraft";
import type {SemanticMaterialCollection,SemanticMaterialDetail,SemanticAnnotationSummary,SemanticAnnotationPage,SemanticAnnotationPreview,SemanticAnnotationDetail} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";
import {useRecoverableMutation} from "../useRecoverableMutation";
import {recoveryKey} from "../reviewRecovery";
import MutationRecovery from "./MutationRecovery";
import {Field,JsonDetails} from "./ui";
import SemanticEvaluationSets from "./SemanticEvaluationSets";

export default function SemanticMaterials({workspaceScope}:{workspaceScope:WorkspaceRequestScope}){
  const [materials,setMaterials]=useState<SemanticMaterialCollection|null>(null),[summary,setSummary]=useState<SemanticAnnotationSummary|null>(null),[selected,setSelected]=useState(""),[error,setError]=useState(""),[refresh,setRefresh]=useState(0);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setError("");
    void Promise.all([api<SemanticMaterialCollection>("/semantic-materials",{signal:abort.signal}),api<SemanticAnnotationSummary>("/semantic-annotations/summary",{signal:abort.signal})]).then(([m,s])=>{if(workspaceScope.isCurrent(scope,abort.signal)){setMaterials(m);setSummary(s);setSelected(v=>v||m.items[0]?.case_id||"");}}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal)){setMaterials(null);setSummary(null);setError(describeError(e));}});return()=>abort.abort();
  },[workspaceScope,refresh]);
  const material=materials?.items.find(m=>m.case_id===selected);
  return <><section className="panel" aria-label="语义材料目录"><h2>原文依据 → 提案 → 五维判断 → 标注记录</h2><p>这是固定评测材料的独立标注。你的判断不会批准实验或结论，也不会改写原材料；程序核验引用身份，不替代语义判断。</p>
    {error&&<p role="alert">{error}</p>}
    {summary&&<p>材料 {summary.material_cases} 项；已有人工声明覆盖 {summary.human_annotated_cases} 项，待确认 {summary.pending_cases} 项；自动化记录 {summary.automation_records} 条。语义质量成绩仍未测量。</p>}
    <Field label="选择语义评测材料"><select value={selected} onChange={e=>setSelected(e.target.value)}>{materials?.items.map(m=><option key={m.case_id} value={m.case_id}>{m.case_id}</option>)}</select></Field>
    <button className="button small" onClick={()=>setRefresh(n=>n+1)}>刷新材料与覆盖</button></section>
    {material&&<MaterialReview key={`${workspaceScope.key}:${material.case_id}:${material.material_sha256}`} material={material} workspaceScope={workspaceScope} onSaved={()=>setRefresh(n=>n+1)}/>}
    {materials&&<SemanticEvaluationSets key={`${workspaceScope.key}:${materials.material_sha256}:${refresh}`} materials={materials} workspaceScope={workspaceScope}/>}
  </>;
}

function MaterialReview({material,workspaceScope,onSaved}:{material:SemanticMaterialDetail;workspaceScope:WorkspaceRequestScope;onSaved:()=>void}){
  const [draft,setDraft]=useState<SemanticDraft>(emptySemanticDraft),[offered,setOffered]=useState<SemanticDraft|null>(null),[draftReady,setDraftReady]=useState(false),[draftError,setDraftError]=useState("");
  const [preview,setPreview]=useState<SemanticAnnotationPreview|null>(null),[previewBody,setPreviewBody]=useState<Record<string,unknown>|null>(null),[history,setHistory]=useState<SemanticAnnotationPage|null>(null),[offset,setOffset]=useState(0),[refresh,setRefresh]=useState(0),[saved,setSaved]=useState<SemanticAnnotationDetail|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const mounted=useRef(false),sending=useRef(false),epoch=useRef(0);
  const key=recoveryKey(workspaceScope.workspaceId,"semantic-material-draft",material.case_id,material.material_sha256);
  const mutation=useRecoverableMutation<SemanticAnnotationDetail>(recoveryKey(workspaceScope.workspaceId,"semantic-material-submit",material.case_id,material.material_sha256),"/semantic-annotations");
  useEffect(()=>{mounted.current=true;try{setOffered(readSemanticDraft(localStorage,key,material.material_sha256));setDraftReady(true);}catch(e){setDraftError(describeError(e));}return()=>{mounted.current=false;epoch.current++;};},[key,material.material_sha256]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setHistory(null);
    void api<SemanticAnnotationPage>(`/semantic-annotations?case_id=${material.case_id}&limit=20&offset=${offset}`,{signal:abort.signal}).then(v=>{if(workspaceScope.isCurrent(scope,abort.signal))setHistory(v);}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[material.case_id,offset,refresh,workspaceScope]);
  function update(next:SemanticDraft){try{saveSemanticDraft(localStorage,key,material.material_sha256,next);setDraft(next);epoch.current++;setPreview(null);setPreviewBody(null);setError("");}catch(e){setDraftError(describeError(e));setDraftReady(false);}}
  function clear(){try{localStorage.removeItem(key);if(localStorage.getItem(key)!==null)throw new Error("无法清理本机草稿。");setDraft(emptySemanticDraft());setOffered(null);setDraftError("");setDraftReady(true);epoch.current++;setPreview(null);setPreviewBody(null);}catch(e){setDraftError(describeError(e));}}
  async function check(){if(sending.current)return;sending.current=true;setBusy(true);setError("");setPreview(null);setPreviewBody(null);const scope=workspaceScope.capture(),seq=++epoch.current;
    const body={material_sha256:material.material_sha256,case_id:material.case_id,source:draft.source,reviewer:draft.reviewer.trim(),confirmed_at:draft.confirmed_at,dimensions:draft.dimensions,supersedes_id:draft.supersedes_id||null};
    try{const value=await post<SemanticAnnotationPreview>("/semantic-annotations/preview",body);if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current){setPreview(value);setPreviewBody(body);}}catch(e){if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current)setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  async function save(body?:Record<string,unknown>){if(sending.current)return;sending.current=true;setBusy(true);setError("");const scope=workspaceScope.capture();
    try{const value=await mutation.execute(body);if(!mounted.current||!workspaceScope.isCurrent(scope))return;setSaved(value);setOffset(0);setRefresh(n=>n+1);mutation.acknowledge(()=>{
      const local=readSemanticDraft(localStorage,key,material.material_sha256);
      if(local&&local.source===value.source&&local.reviewer.trim()===value.reviewer&&local.confirmed_at===value.confirmed_at&&JSON.stringify(local.dimensions)===JSON.stringify(value.dimensions)&&(local.supersedes_id||null)===value.supersedes_id)clear();
    });onSaved();}catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  const locked=busy||!draftReady||!!draftError||!!offered||!mutation.ready||mutation.blocked||!!mutation.pending;
  const complete=!!draft.source&&!!draft.reviewer.trim()&&!!draft.confirmed_at&&Object.values(draft.dimensions).every(v=>!!v.outcome&&!!v.reason.trim());
  return <section className="panel semantic-material" aria-label="语义材料标注" data-material-id={material.case_id}><h2>{material.case_id}</h2>
    <h3>待判断的提案</h3>{Object.entries(material.proposal).map(([key,value])=><p key={key}><strong>{key}：</strong>{typeof value==="string"?value:JSON.stringify(value)}</p>)}
    <h3>引用依据</h3>{material.evidence.map((e,i)=><article key={i}><p>证据 {String(e.id)} · 页码 / 定位 {String(e.page??e.locator??"见来源记录")}</p><blockquote>{String(e.quote??e.text??JSON.stringify(e))}</blockquote></article>)}
    {material.sources.map(s=><p key={s.source_id}><a target="_blank" rel="noreferrer" href={`/api/semantic-material-sources/${s.source_id}`}>打开来源 {s.source_id}</a> · {s.raw_pdf_reverified?"本地论文摘要已核验":"来源登记文件，未重新核验原论文 PDF"}</p>)}
    <details><summary>查看未经人工确认的参考提示</summary><p>{material.reference_draft}</p></details>
    <JsonDetails label="材料与来源的准确身份" value={{material_sha256:material.material_sha256,material_case_digest:material.material_case_digest,sources:material.sources}}/>
    {error&&<p role="alert">{error}</p>}{draftError&&<p role="alert">{draftError}</p>}
    {(offered||draftError)&&<section aria-label="标注草稿恢复"><p>此材料存在本机草稿或存储错误，请先恢复或明确清理；不会自动提交。</p>{offered&&<button className="button" disabled={busy} onClick={()=>{setDraft(offered);setOffered(null);}}>恢复标注草稿</button>}<button className="button" disabled={busy} onClick={clear}>清理本机标注草稿</button></section>}
    <MutationRecovery name="材料标注" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void save()} onDismiss={mutation.dismissRejected}/>
    <fieldset disabled={locked}><legend>填写自己的判断</legend>
      <Field label="标注声明来源"><select value={draft.source} onChange={e=>update({...draft,source:e.target.value as SemanticDraft["source"]})}><option value="">请明确选择</option><option value="human">本人实际阅读后的判断</option><option value="automation">自动化测试记录</option></select></Field>
      <Field label="材料审核者标识"><input value={draft.reviewer} maxLength={120} onChange={e=>update({...draft,reviewer:e.target.value})}/></Field>
      <Field label="实际确认时间（含时区）"><input value={draft.confirmed_at} maxLength={40} placeholder="例如 2026-10-05T14:30:00+01:00" onChange={e=>update({...draft,confirmed_at:e.target.value})}/></Field>
      <button className="button small" onClick={()=>update({...draft,confirmed_at:new Date().toISOString()})}>记录当前确认时间</button>
      {Object.entries(assessmentDimensions).map(([name,label])=>{const key=name as Dimension,v=draft.dimensions[key];return <div key={key}><Field label={`${label}判断`}><select value={v.outcome} onChange={e=>update({...draft,dimensions:{...draft.dimensions,[key]:{...v,outcome:e.target.value}}})}><option value="">请选择自己的判断</option><option value="passed">通过</option><option value="failed">未通过</option><option value="not_assessed">尚未评定</option><option value="not_applicable">不适用</option></select></Field><Field label={`${label}理由`}><textarea value={v.reason} maxLength={2000} rows={2} onChange={e=>update({...draft,dimensions:{...draft.dimensions,[key]:{...v,reason:e.target.value}}})}/></Field></div>;})}
      <details><summary>修改已有标注时的准确记录身份</summary><Field label="取代原标注 ID"><input value={draft.supersedes_id} maxLength={100} onChange={e=>update({...draft,supersedes_id:e.target.value})}/></Field><p>只允许取代同材料、同声明来源、同审核者的最新记录；历史继续保留。</p></details>
      <div className="button-row"><button className="button" disabled={!complete} onClick={()=>void check()}>预检材料标注</button><button className="button primary" disabled={!previewBody} onClick={()=>{if(previewBody)void save(previewBody);}}>保存自己的材料标注</button></div>
    </fieldset>
    {preview&&<JsonDetails label="待提交的准确标注及权限边界" value={preview}/>}
    {saved&&<section aria-label="已保存材料标注"><p>已保存 {saved.id} · {saved.source} · {saved.declared_semantic_status}。身份为本地声明，不代表实验或结论审批。</p><a href={`/api/semantic-annotations/${saved.id}/export`} download>下载本次标注记录</a></section>}
    <h3>保留的标注历史</h3><p>共 {history?.total??"…"} 条；自动化不计为人工标签。以下均为历史声明，最新有效覆盖见材料目录。</p>
    <div className="button-row"><button className="button small" disabled={!offset} onClick={()=>setOffset(n=>Math.max(0,n-20))}>上一页标注</button><button className="button small" disabled={!history||offset+20>=history.total} onClick={()=>setOffset(n=>n+20)}>下一页标注</button></div>
    {history?.items.map(a=><article key={a.id}><p>{formatDate(a.created_at)} · {a.source} · {a.reviewer} · {a.declared_semantic_status}</p><JsonDetails label={`查看标注 ${a.id}`} value={a}/><a href={`/api/semantic-annotations/${a.id}/export`} download>下载历史标注</a></article>)}
  </section>;
}
