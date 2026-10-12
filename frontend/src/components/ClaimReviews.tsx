import {useEffect,useRef,useState} from "react";
import {api,post} from "../api";
import {describeError} from "../domain";
import {emptySemanticDraft,readSemanticDraft,saveSemanticDraft} from "../semanticAnnotationDraft";
import type {SemanticDraft} from "../semanticAnnotationDraft";
import type {ClaimDetail,ClaimReviewTarget,ClaimReviewPreview,ClaimReviewDetail,ClaimReviewPage,ClaimReviewStatus,ReviewMaterialsDetail} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";
import {useRecoverableMutation} from "../useRecoverableMutation";
import {recoveryKey} from "../reviewRecovery";
import MutationRecovery from "./MutationRecovery";
import ExplicitDimensions from "./ExplicitDimensions";
import {Field,JsonDetails} from "./ui";

export default function ClaimReviews({claims,workspaceScope}:{claims:ClaimDetail;workspaceScope:WorkspaceRequestScope}){
  const [selected,setSelected]=useState(claims.claims[0]?.id||""),[target,setTarget]=useState<ClaimReviewTarget|null>(null),[error,setError]=useState("");
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setTarget(null);setError("");
    void api<ClaimReviewTarget>(`/claim-review-targets/${claims.id}?claim_id=${encodeURIComponent(selected)}`,{signal:abort.signal}).then(v=>{if(workspaceScope.isCurrent(scope,abort.signal))setTarget(v);}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[claims.id,selected,workspaceScope]);
  return <section aria-label="精确结论审核" className="semantic-material"><h3>审核这一次的结论文字</h3>
    <p>工具已核对数值引用；以下语义判断只绑定选中的准确文字版本。新文字需要重新审核，材料标签和原实验审核不会迁移到这里。</p>
    <Field label="选择具体结论"><select value={selected} onChange={e=>setSelected(e.target.value)}>{claims.claims.map(c=><option key={c.id} value={c.id}>{c.id}</option>)}</select></Field>
    {error&&<p role="alert">{error}</p>}
    {target&&<ClaimReviewForm key={`${workspaceScope.key}:${target.target_digest}`} target={target} workspaceScope={workspaceScope}/>}
  </section>;
}

function ClaimReviewForm({target,workspaceScope}:{target:ClaimReviewTarget;workspaceScope:WorkspaceRequestScope}){
  const [draft,setDraft]=useState<SemanticDraft>(emptySemanticDraft),[offered,setOffered]=useState<SemanticDraft|null>(null),[ready,setReady]=useState(false);
  const [preview,setPreview]=useState<ClaimReviewPreview|null>(null),[body,setBody]=useState<Record<string,unknown>|null>(null),[saved,setSaved]=useState<ClaimReviewDetail|null>(null);
  const [page,setPage]=useState<ClaimReviewPage|null>(null),[status,setStatus]=useState<ClaimReviewStatus|null>(null),[offset,setOffset]=useState(0),[refresh,setRefresh]=useState(0),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const mounted=useRef(false),sending=useRef(false),epoch=useRef(0);
  const key=recoveryKey(workspaceScope.workspaceId,"claim-review-draft",target.target_digest);
  const mutation=useRecoverableMutation<ClaimReviewDetail>(recoveryKey(workspaceScope.workspaceId,"claim-review-submit",target.target_digest),"/claim-reviews");
  const query=`claims_id=${target.claims_id}&claim_id=${encodeURIComponent(target.claim_id)}`;
  useEffect(()=>{mounted.current=true;try{setOffered(readSemanticDraft(localStorage,key,target.target_digest));setReady(true);}catch(e){setError(describeError(e));}return()=>{mounted.current=false;epoch.current++;};},[key,target.target_digest]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setPage(null);setStatus(null);
    void Promise.all([api<ClaimReviewPage>(`/claim-reviews?${query}&limit=20&offset=${offset}`,{signal:abort.signal}),api<ClaimReviewStatus>(`/claim-reviews/status?${query}`,{signal:abort.signal})]).then(([p,s])=>{if(workspaceScope.isCurrent(scope,abort.signal)){setPage(p);setStatus(s);}}).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[query,offset,refresh,workspaceScope]);
  function update(next:SemanticDraft){try{saveSemanticDraft(localStorage,key,target.target_digest,next);setDraft(next);epoch.current++;setPreview(null);setBody(null);setError("");}catch(e){setError(describeError(e));setReady(false);}}
  function clear(){try{localStorage.removeItem(key);if(localStorage.getItem(key)!==null)throw new Error("无法清理结论审核草稿。");setDraft(emptySemanticDraft());setOffered(null);setReady(true);setError("");epoch.current++;setPreview(null);setBody(null);}catch(e){setError(describeError(e));}}
  async function check(){if(sending.current)return;sending.current=true;setBusy(true);setError("");setPreview(null);setBody(null);const scope=workspaceScope.capture(),seq=++epoch.current;
    const parent=page?.items.find(r=>r.id===draft.supersedes_id);
    const request={claims_id:target.claims_id,claim_id:target.claim_id,expected_target_digest:target.target_digest,source:draft.source,reviewer:draft.reviewer.trim(),confirmed_at:draft.confirmed_at,dimensions:draft.dimensions,supersedes_id:draft.supersedes_id||null,expected_supersedes_digest:parent?.digest||null};
    try{const v=await post<ClaimReviewPreview>("/claim-reviews/preview",request);if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current){setPreview(v);setBody(request);}}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(scope)&&seq===epoch.current)setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  async function save(request?:Record<string,unknown>){if(sending.current)return;sending.current=true;setBusy(true);setError("");const scope=workspaceScope.capture();
    try{const v=await mutation.execute(request);if(mounted.current&&workspaceScope.isCurrent(scope)){setSaved(v);setOffset(0);setRefresh(n=>n+1);mutation.acknowledge(clear);}}
    catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  const locked=busy||!ready||!!offered||!mutation.ready||!!mutation.pending||mutation.blocked;
  const complete=!!draft.source&&!!draft.reviewer.trim()&&!!draft.confirmed_at&&Object.values(draft.dimensions).every(d=>!!d.outcome&&!!d.reason.trim());
  return <div data-target-digest={target.target_digest}>
    <p><strong>本次待审核文字：</strong>{target.claim.narrative_text}</p><p><strong>服务计算的实际值：</strong>{target.claim.authoritative_display||"此项没有数值引用"}</p>
    <p>数据范围：{target.data_scope}</p><p>方法范围：{target.method_scope}</p>
    <ReviewMaterialLinks target={target} workspaceScope={workspaceScope}/>
    {target.evidence.map(e=><p key={e.id}>证据 {e.id} · {e.locator} · {e.text}</p>)}
    <JsonDetails label="准确结论、来源与执行身份" value={target}/>
    {status&&<p>本版本人工声明：{status.human_declared_status}；人工 {status.human_records} 条，自动化 {status.automation_records} 条。模型质量尚未测量。</p>}
    {error&&<p role="alert">{error}</p>}
    {(!ready||offered)&&<div><p>此版本存在本机草稿或存储错误，请先恢复或清理。</p>{offered&&<button className="button" onClick={()=>{setDraft(offered);setOffered(null);}}>恢复结论审核草稿</button>}<button className="button" onClick={clear}>清理结论审核草稿</button></div>}
    <MutationRecovery name="结论审核" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void save()} onDismiss={mutation.dismissRejected}/>
    <details><summary>填写本版本的五维判断</summary><fieldset disabled={locked}>
      <Field label="结论审核声明来源"><select value={draft.source} onChange={e=>update({...draft,source:e.target.value as SemanticDraft["source"]})}><option value="">请明确选择</option><option value="human">本人实际审核后的判断</option><option value="automation">自动化测试记录</option></select></Field>
      <Field label="结论审核者标识"><input value={draft.reviewer} maxLength={120} onChange={e=>update({...draft,reviewer:e.target.value})}/></Field>
      <Field label="结论确认时间（含时区）"><input value={draft.confirmed_at} maxLength={40} onChange={e=>update({...draft,confirmed_at:e.target.value})}/></Field><button className="button small" onClick={()=>update({...draft,confirmed_at:new Date().toISOString()})}>记录结论确认时间</button>
      <ExplicitDimensions prefix="结论" value={draft.dimensions} onChange={dimensions=>update({...draft,dimensions})}/>
      <Field label="修正已有结论审核"><select value={draft.supersedes_id} onChange={e=>update({...draft,supersedes_id:e.target.value})}><option value="">新判断</option>{page?.items.filter(r=>r.source===draft.source&&r.reviewer===draft.reviewer.trim()&&[...(status?.active_human_review_ids??[]),...(status?.active_automation_review_ids??[])].includes(r.id)).map(r=><option key={r.id} value={r.id}>{r.created_at} · {r.declared_semantic_status}</option>)}</select></Field>
      <div className="button-row"><button className="button" disabled={!complete} onClick={()=>void check()}>预检本版本结论审核</button><button className="button primary" disabled={!body} onClick={()=>{if(body)void save(body);}}>保存本版本结论审核</button></div>
    </fieldset></details>
    {preview&&<JsonDetails label="待保存的版本审核" value={preview}/>}
    {saved&&<p role="status">已保存结论审核：{saved.id} · {saved.source} · {saved.declared_semantic_status}</p>}
    <details><summary>本版本审核历史</summary><p>共 {page?.total??"…"} 条；取代保留原记录。身份是本地声明。</p>
      <div className="button-row"><button className="button small" disabled={!offset} onClick={()=>setOffset(v=>Math.max(0,v-20))}>上一页结论审核</button><button className="button small" disabled={!page||offset+20>=page.total} onClick={()=>setOffset(v=>v+20)}>下一页结论审核</button></div>
      {page?.items.map(r=><article key={r.id}><p>{r.reviewer} · {r.source} · {r.declared_semantic_status}</p><JsonDetails label={`准确审核 ${r.id}`} value={r}/><a href={`/api/claim-reviews/${r.id}/export`} download>下载该次结论审核</a></article>)}
    </details>
  </div>;
}

function ReviewMaterialLinks({target,workspaceScope}:{target:ClaimReviewTarget;workspaceScope:WorkspaceRequestScope}){
  const [materials,setMaterials]=useState<ReviewMaterialsDetail|null>(null),[error,setError]=useState("");
  useEffect(()=>{
    const scope=workspaceScope.capture(),abort=new AbortController();setMaterials(null);setError("");
    const query=new URLSearchParams({claim_id:target.claim_id,expected_target_digest:target.target_digest});
    void api<ReviewMaterialsDetail>(`/review-materials/${target.claims_id}?${query}`,{signal:abort.signal}).then(value=>{
      if(!workspaceScope.isCurrent(scope,abort.signal))return;
      if(value.target.target_digest!==target.target_digest||value.target.claims_id!==target.claims_id||value.target.claim_id!==target.claim_id
        ||value.case.id!==target.case_id||value.case.digest!==target.case_digest||value.case.source_digest!==target.source_digest
        ||value.claims.id!==target.claims_id||value.claims.digest!==target.claims_digest)throw new Error("来源材料属于其他结论版本，请刷新后重试。");
      const ids=new Set<string>();
      for(const source of value.sources){
        const url=new URL(source.download_url,window.location.origin);
        if(ids.has(source.id)||url.origin!==window.location.origin||url.hash
          ||url.pathname!==`/api/review-materials/${target.claims_id}/sources/${source.id}`
          ||[...url.searchParams].length!==2||url.searchParams.get("claim_id")!==target.claim_id
          ||url.searchParams.get("expected_target_digest")!==target.target_digest)throw new Error("来源链接未绑定本次准确目标。");
        ids.add(source.id);
      }
      setMaterials(value);
    }).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[target.target_digest,target.claims_id,target.claim_id,target.case_id,target.case_digest,target.source_digest,target.claims_digest,workspaceScope]);
  return <section aria-label="本版本原始审核资料">
    <p>先打开原论文，按证据定位阅读；服务计算的实际值在上方，完整输出在研究任务详情中。查看资料不会提交人工判断。</p>
    {!materials&&!error&&<p>正在核验本版本原始来源…</p>}
    {error&&<p role="status">原始资料暂不可用：{error}。已有判断草稿仍保留。</p>}
    {materials&&<ul>{materials.sources.map(source=><li key={source.id}>
      <a href={source.download_url} target="_blank" rel="noreferrer">{source.media_type==="application/pdf"?"打开原论文":"查看来源文件"}：{source.label}</a>
      <details><summary>文件身份与引用范围</summary><p>{source.filename} · {source.size} bytes · SHA256 {source.sha256}</p><p>对应证据：{source.evidence_ids.join("、")||"无"}</p></details>
    </li>)}</ul>}
  </section>;
}
