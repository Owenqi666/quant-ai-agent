import {useEffect,useRef,useState} from 'react';
import {api} from '../api';
import {describeError,shortId} from '../domain';
import type {PublicMaterialDetail,PublicMaterialSummary,PublicMaterialVersion,IndustryMomSourcePage} from '../generated/api-contract';
import type {WorkspaceRequestScope} from '../workspaceScope';
import {useI18n} from '../i18n';
import {useRecoverableMutation} from '../useRecoverableMutation';
import {recoveryKey} from '../reviewRecovery';
import {assertExperimentMaterialLineage,assertMaterialPlan,emptyMonthlyReview,industryMaterialPlanBase,materialPlanPath,monthlyReviewBody,monthlyReviewChecks,materialRequestKey,monthlyReviewDigest} from '../industryMomMaterialPlans';
import type {IndustryExperimentMaterialLineage,IndustryMaterialPlan,IndustryMaterialPlanPage,IndustryMaterialLineage,MaterialPlanRequest,MonthlyReviewDraft} from '../industryMomMaterialPlans';
import MutationRecovery from './MutationRecovery';
import {Field,JsonDetails} from './ui';

type PlanSaved={plan:IndustryMaterialPlan;replayed:boolean};
type PlanStarted={plan_id:string;plan_digest:string;experiment_id:string;replayed:boolean};
const checkLabels = {
  citation_accuracy_checked:['所选引文与页码已核对','I checked selected quotations and physical pages'],
  hypothesis_fidelity_checked:['假说与所引原文已核对','I checked the hypothesis against the cited source'],
  monthly_return_semantics_checked:['月度收益单位及形成时序已核对','I checked monthly return units and formation timing'],
  scope_modifications_checked:['行业替代、权重、缺失规则及费用范围已核对','I checked industry substitution, weights, missing rules and cost scope'],
  no_unsupported_field_substitution:['未作未经说明的字段替代','No undeclared field substitution was used'],
} as const;
const planText=(value:unknown)=>typeof value==='string'||typeof value==='number'||typeof value==='boolean'?String(value):'—';
const bodyKey=materialRequestKey;

export default function IndustryMomMaterialPlans({workspaceScope,onOpenExperiment,refresh=0}:{workspaceScope:WorkspaceRequestScope;onOpenExperiment:(id:string)=>void;refresh?:number}){
  const {t}=useI18n();
  const [materials,setMaterials]=useState<PublicMaterialSummary[]>([]),[sources,setSources]=useState<IndustryMomSourcePage|null>(null);
  const [materialOffset,setMaterialOffset]=useState(0),[materialTotal,setMaterialTotal]=useState(0),[sourceOffset,setSourceOffset]=useState(0);
  const [materialId,setMaterialId]=useState(''),[versionId,setVersionId]=useState(''),[sourceId,setSourceId]=useState('');
  const [material,setMaterial]=useState<PublicMaterialDetail|null>(null),[version,setVersion]=useState<PublicMaterialVersion|null>(null);
  const [draft,setDraft]=useState<MonthlyReviewDraft>(emptyMonthlyReview),[preview,setPreview]=useState<IndustryMaterialPlan|null>(null),[previewBody,setPreviewBody]=useState<MaterialPlanRequest|null>(null);
  const [selected,setSelected]=useState<{id:string;digest:string}|null>(null),[plans,setPlans]=useState<IndustryMaterialPlanPage|null>(null),[planOffset,setPlanOffset]=useState(0);
  const [loading,setLoading]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[reload,setReload]=useState(0),[freezeConfirmed,setFreezeConfirmed]=useState(false);
  const mounted=useRef(false),epoch=useRef(0),sending=useRef(false),formRef=useRef(''),previewAbort=useRef<AbortController|null>(null);
  const previewMutation=useRecoverableMutation<IndustryMaterialPlan>(recoveryKey(workspaceScope.workspaceId,'industry-material-plan-preview'),`${industryMaterialPlanBase}/preview`);
  const createMutation=useRecoverableMutation<PlanSaved>(recoveryKey(workspaceScope.workspaceId,'industry-material-plan-create'),industryMaterialPlanBase);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;epoch.current++;previewAbort.current?.abort();};},[]);
  function clearReview(){epoch.current++;setDraft(emptyMonthlyReview());setPreview(null);setPreviewBody(null);setFreezeConfirmed(false);setError('');}
  function edit(next:MonthlyReviewDraft){epoch.current++;setDraft(next);setPreview(null);setPreviewBody(null);setFreezeConfirmed(false);}
  useEffect(()=>{
    const scope=workspaceScope.capture(),abort=new AbortController();setLoading(true);setError('');
    void Promise.all([api<{items:PublicMaterialSummary[];total:number}>(`/research-v2/materials?limit=20&offset=${materialOffset}`,{signal:abort.signal}),
      api<IndustryMomSourcePage>(`/industry-mom-sources?limit=20&offset=${sourceOffset}`,{signal:abort.signal}),
      api<IndustryMaterialPlanPage>(`${industryMaterialPlanBase}?limit=20&offset=${planOffset}`,{signal:abort.signal})]).then(([m,s,p])=>{
      if(workspaceScope.isCurrent(scope,abort.signal)){setMaterials(m.items);setMaterialTotal(m.total);setSources(s);setPlans(p);}
    }).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));}).finally(()=>{if(workspaceScope.isCurrent(scope,abort.signal))setLoading(false);});
    return()=>abort.abort();
  },[materialOffset,sourceOffset,planOffset,reload,refresh,workspaceScope]);
  useEffect(()=>{
    const scope=workspaceScope.capture(),abort=new AbortController();setMaterial(null);setVersion(null);
    if(materialId)void api<PublicMaterialDetail>(`/research-v2/materials/${encodeURIComponent(materialId)}`,{signal:abort.signal}).then(v=>{
      if(!workspaceScope.isCurrent(scope,abort.signal))return;if(v.id!==materialId)throw Error(t('材料身份不匹配。','Material identity differs.'));
      setMaterial(v);setVersionId(v.latest_version?.id||'');
    }).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});
    return()=>abort.abort();
  },[materialId,workspaceScope]);
  useEffect(()=>{
    const scope=workspaceScope.capture(),abort=new AbortController();setVersion(null);
    if(materialId&&versionId)void api<PublicMaterialVersion>(`/research-v2/materials/${encodeURIComponent(materialId)}/versions/${encodeURIComponent(versionId)}`,{signal:abort.signal}).then(v=>{
      if(!workspaceScope.isCurrent(scope,abort.signal))return;if(v.material_id!==materialId||v.id!==versionId)throw Error(t('采纳版本不属于当前材料。','Adopted version belongs to another material.'));setVersion(v);
    }).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));});return()=>abort.abort();
  },[materialId,versionId,workspaceScope]);
  const source=sources?.items.find(item=>item.id===sourceId);
  let currentBody:MaterialPlanRequest|null=null;
  try{if(version&&source)currentBody=monthlyReviewBody(draft,{materialId,versionId,materialDigest:version.digest,sourceId,sourceDigest:source.digest,material:version.material});}catch{/* explicit fields below show missing decisions */}
  const currentKey=bodyKey(currentBody);formRef.current=currentKey;
  const previewCurrent=!!preview&&!!previewBody&&bodyKey(previewBody)===currentKey;
  const locked=busy||!previewMutation.ready||previewMutation.blocked||!!previewMutation.pending||!createMutation.ready||createMutation.blocked||!!createMutation.pending;
  async function check(body?:MaterialPlanRequest){
    if(sending.current)return;sending.current=true;setBusy(true);setError('');setPreview(null);setPreviewBody(null);setFreezeConfirmed(false);
    const scope=workspaceScope.capture(),sequence=++epoch.current;const original=body||(previewMutation.pending?.body as unknown as MaterialPlanRequest);
    const requestKey=bodyKey({review:original.review,source_id:original.source_id,source_digest:original.source_digest});
    const abort=new AbortController();previewAbort.current=abort;let timedOut=false;const timer=setTimeout(()=>{timedOut=true;abort.abort();},15000);
    try{const value=await previewMutation.execute(body as unknown as Record<string,unknown>|undefined,undefined,{signal:abort.signal});
      if(!mounted.current||!workspaceScope.isCurrent(scope)||sequence!==epoch.current||abort.signal.aborted)return;
      assertMaterialPlan(value,undefined,original,await monthlyReviewDigest(original.review));
      if(!mounted.current||!workspaceScope.isCurrent(scope)||sequence!==epoch.current||abort.signal.aborted)return;
      setPreview(value);setPreviewBody({review:original.review,source_id:original.source_id,source_digest:original.source_digest});
      previewMutation.acknowledge();
      if(formRef.current!==requestKey)setError(t('已读取原请求的预览。当前表单不同，不能用此预览保存；请按所需材料重新填写审核并预览。','The original preview was recovered. The current form differs, so it cannot be saved. Review the required selection and preview it again.'));
    }catch(e){if(mounted.current&&workspaceScope.isCurrent(scope)&&sequence===epoch.current)setError(abort.signal.aborted?(timedOut?t('固定计划预览读取超时；原请求保留，可安全重试。','Fixed-plan preview timed out; the original request is retained for safe retry.'):t('已取消本次预览读取；原请求保留，不生成计划或实验。','Preview reading was cancelled; the original request is retained and no plan or experiment is created.')):describeError(e));}
    finally{clearTimeout(timer);if(previewAbort.current===abort)previewAbort.current=null;sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  async function freeze(body?:Record<string,unknown>){
    if(sending.current)return;sending.current=true;setBusy(true);setError('');const scope=workspaceScope.capture();
    try{const request=body||(createMutation.pending?.body as Record<string,unknown>);
      const saved=await createMutation.execute(body);if(!mounted.current||!workspaceScope.isCurrent(scope))return;
      assertMaterialPlan(saved.plan,{id:String(request.expected_plan_id),digest:String(request.expected_plan_digest)},request as unknown as MaterialPlanRequest,await monthlyReviewDigest((request as unknown as MaterialPlanRequest).review));
      if(!mounted.current||!workspaceScope.isCurrent(scope))return;
      setSelected({id:saved.plan.plan_id,digest:saved.plan.plan_digest});createMutation.acknowledge(()=>{setPreview(null);setPreviewBody(null);setFreezeConfirmed(false);});setReload(v=>v+1);
    }catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}
  }
  const evidence=version?.material.evidence.filter(e=>version.material.hypotheses.find(h=>h.id===draft.hypothesisId)?.evidence_ids.includes(e.id))||[];
  return <section className="panel" aria-label={t('材料到行业 MOM 固定计划','Material to fixed industry MOM plan')} style={{overflowWrap:'anywhere'}}>
    <h2>{t('材料 → 明确审核 → 固定计划 → 实验','Material → explicit review → fixed plan → experiment')}</h2>
    <p>{t('这里复用已采纳的材料和固定行业方法，不请求模型，不生成任意月频公式。计算结果和人工研究判断分别记录。','This reuses adopted material and a fixed industry method. It calls no model and generates no arbitrary monthly formula. Computation and human research judgments are recorded separately.')}</p>
    {error&&<p role="alert">{error}</p>}{loading&&<p role="status">{t('正在读取材料、来源与固定计划…','Reading materials, sources and fixed plans…')}</p>}
    <button type="button" className="button small" disabled={busy} onClick={()=>setReload(v=>v+1)}>{t('重新读取材料和固定计划','Reload materials and fixed plans')}</button>
    {busy&&previewAbort.current&&<button type="button" className="button small" onClick={()=>previewAbort.current?.abort()}>{t('取消预览读取','Cancel preview reading')}</button>}
    <MutationRecovery name={t('固定计划预览','fixed plan preview')} pending={previewMutation.pending} error={previewMutation.error} busy={busy} onRetry={()=>void check()} onDismiss={previewMutation.dismissRejected}/>
    <MutationRecovery name={t('固定计划保存','fixed plan save')} pending={createMutation.pending} error={createMutation.error} busy={busy} onRetry={()=>void freeze()} onDismiss={createMutation.dismissRejected}/>
    <details><summary>{t('从已采纳材料建立固定计划','Create a fixed plan from adopted material')}</summary><fieldset disabled={locked}>
      <Field label={t('选择研究材料','Choose research material')}><select value={materialId} onChange={e=>{setMaterialId(e.target.value);setVersionId('');clearReview();}}><option value="">{t('请明确选择','Choose explicitly')}</option>{materials.map(m=><option key={m.id} value={m.id}>{m.title} · {shortId(m.id)}</option>)}</select></Field>
      <div className="button-row"><button type="button" className="button small" disabled={!materialOffset} onClick={()=>setMaterialOffset(v=>Math.max(0,v-20))}>{t('上一页材料','Previous materials')}</button><button type="button" className="button small" disabled={materialOffset+20>=materialTotal} onClick={()=>setMaterialOffset(v=>v+20)}>{t('下一页材料','Next materials')}</button></div>
      {!loading&&!materials.length&&<p>{t('没有已导入材料；先在材料输入流程完成抽取和明确采纳。','No imported material. Complete extraction and explicit adoption in the materials flow first.')}</p>}
      {materialId&&<Field label={t('采纳版本 ID（可读取保留的历史版本）','Adopted version ID (retained history can be read)')}><input value={versionId} maxLength={128} onChange={e=>{setVersionId(e.target.value);clearReview();}}/></Field>}
      {material&&!material.latest_version&&<p role="status">{t('此材料尚未采纳；不能跳过采纳建立计划。','This material has not been adopted; adoption cannot be skipped.')}</p>}
      {materialId&&versionId&&!version&&!error&&<p role="status">{t('正在核验准确材料版本…','Verifying the exact material version…')}</p>}
      {version&&<><p>{t('已采纳版本','Adopted version')} {version.version} · <code>{version.id}</code> · {t('语义仍未独立评定','Semantics remain independently unassessed')}</p>
        <Field label={t('选择本次假说','Choose this hypothesis')}><select value={draft.hypothesisId} onChange={e=>edit({...draft,hypothesisId:e.target.value,evidenceIds:[],checks:emptyMonthlyReview().checks})}><option value="">{t('请明确选择假说','Choose a hypothesis explicitly')}</option>{version.material.hypotheses.map(h=><option key={h.id} value={h.id}>{h.id} · {h.claim.slice(0,110)}</option>)}</select></Field>
        {!!version.material.unresolved_rules.length&&<p role="status">{t('材料级未决规则会阻断计划','Material unresolved rules block the plan')}: {version.material.unresolved_rules.join('; ')}</p>}
        {draft.hypothesisId&&<AdoptedHypothesis version={version} hypothesisId={draft.hypothesisId}/>}
        <fieldset><legend>{t('明确选择本次审核引用','Explicitly choose citations for this review')}</legend>{evidence.map(e=><label key={e.id} style={{display:'block'}}><input type="checkbox" checked={draft.evidenceIds.includes(e.id)} onChange={event=>edit({...draft,evidenceIds:event.target.checked?[...draft.evidenceIds,e.id]:draft.evidenceIds.filter(id=>id!==e.id)})}/>{e.id} · {t('物理页','Physical page')} {e.page} · {e.quote.slice(0,140)}…</label>)}</fieldset>
      </>}
      <Field label={t('选择本次行业来源','Choose this industry source')}><select value={sourceId} onChange={e=>{setSourceId(e.target.value);clearReview();}}><option value="">{t('请明确选择已注册来源','Choose a registered source explicitly')}</option>{sources?.items.map(s=><option key={s.id} value={s.id}>{s.title} · {shortId(s.id)}</option>)}</select></Field>
      <div className="button-row"><button type="button" className="button small" disabled={!sourceOffset} onClick={()=>{setSourceOffset(v=>Math.max(0,v-20));setSourceId('');clearReview();}}>{t('上一页计划来源','Previous plan sources')}</button><button type="button" className="button small" disabled={!sources||sourceOffset+20>=sources.total} onClick={()=>{setSourceOffset(v=>v+20);setSourceId('');clearReview();}}>{t('下一页计划来源','Next plan sources')}</button></div>
      <p>{t('固定范围：49 行业组合；H−12..H−2 十一个月，跳过 H−1；完整历史、不填补；2010–2011 开发，2012–2013 保留不评估；只算毛收益。个股原论文仍受数据阻断。','Fixed scope: 49 industry portfolios; eleven months H−12..H−2, skipping H−1; complete history, no fill; development 2010–2011, reserved 2012–2013 unevaluated; gross returns only. The original stock study remains data-blocked.')}</p>
      <Field label={t('审核声明来源','Review declaration source')}><select value={draft.reviewSource} onChange={e=>edit({...draft,reviewSource:e.target.value as MonthlyReviewDraft['reviewSource']})}><option value="">{t('请明确选择声明来源','Choose a declaration source explicitly')}</option><option value="user">{t('本人实际审核','My own review')}</option><option value="codex_proxy">{t('Codex 代理审核（不作为真人评价）','Codex proxy review (not a human evaluation)')}</option></select></Field>
      <Field label={t('本次审核者标识','This reviewer ID')}><input value={draft.reviewer} maxLength={100} onChange={e=>edit({...draft,reviewer:e.target.value})}/></Field>
      <Field label={t('本次审核理由与研究改动说明','Review rationale and research modifications')}><textarea value={draft.rationale} maxLength={4000} onChange={e=>edit({...draft,rationale:e.target.value})}/></Field>
      {monthlyReviewChecks.map(key=><label key={key} style={{display:'block'}}><input type="checkbox" checked={draft.checks[key]} onChange={e=>edit({...draft,checks:{...draft.checks,[key]:e.target.checked}})}/>{t(checkLabels[key][0],checkLabels[key][1])}</label>)}
      <button type="button" className="button" disabled={!currentBody} onClick={()=>{if(currentBody)void check(currentBody);}}>{t('核验本次固定计划预览','Verify this fixed plan preview')}</button>
    </fieldset>
    {preview&&<><IndustryMaterialPlanView plan={preview}/><fieldset disabled={locked||!previewCurrent}>
      <label><input type="checkbox" checked={freezeConfirmed} onChange={e=>setFreezeConfirmed(e.target.checked)}/>{t('按上方准确材料版本、审核和来源保存固定计划','Save the fixed plan with this exact material version, review and source')}</label>
      <button type="button" className="button primary" disabled={!previewCurrent||!freezeConfirmed} onClick={()=>{if(previewBody&&preview)void freeze({...previewBody,expected_plan_id:preview.plan_id,expected_plan_digest:preview.plan_digest});}}>{t('保存本次固定计划','Save this fixed plan')}</button>
    </fieldset></>}
    </details>
    <h3>{t('已保存的固定计划','Saved fixed plans')}</h3>{!loading&&plans?.total===0&&<p>{t('尚无材料关联计划；普通历史实验不会自动补来源。','No material-linked plans. Ordinary historical experiments do not receive inferred provenance.')}</p>}
    {plans?.items.map(p=><p key={p.plan_id}><button type="button" className="text-button" onClick={()=>setSelected({id:p.plan_id,digest:p.plan_digest})}>{t('查看固定计划','View fixed plan')} {shortId(p.plan_id)}</button></p>)}
    <div className="button-row"><button type="button" className="button small" disabled={!planOffset} onClick={()=>setPlanOffset(v=>Math.max(0,v-20))}>{t('上一页计划','Previous plans')}</button><button type="button" className="button small" disabled={!plans||planOffset+20>=plans.total} onClick={()=>setPlanOffset(v=>v+20)}>{t('下一页计划','Next plans')}</button></div>
    {selected&&<SavedIndustryMaterialPlan key={`${workspaceScope.key}:${selected.id}:${selected.digest}`} identity={selected} workspaceScope={workspaceScope} onOpenExperiment={onOpenExperiment}/>}
  </section>;
}
function AdoptedHypothesis({version,hypothesisId}:{version:PublicMaterialVersion;hypothesisId:string}){
  const {t}=useI18n(),h=version.material.hypotheses.find(item=>item.id===hypothesisId);if(!h)return null;
  return <article><p><strong>{t('研究主张','Research claim')}:</strong> {h.claim} · {h.claim_attribution}</p><p><strong>{t('原材料计算定义','Material calculation')}:</strong> {h.calculation||t('未提供','Not provided')}</p>
    <p>{t('经济机制','Economic mechanism')}: {h.economic_mechanism||t('未提供；保持未评定','Not provided; remains unassessed')} · {h.mechanism_attribution}</p>
    {!!h.unresolved_rules.length&&<p role="status">{t('假说未决规则','Hypothesis unresolved rules')}: {h.unresolved_rules.join('; ')}</p>}
    <details><summary>{t('阅读所引证据与条件','Read cited evidence and conditions')}</summary>{version.material.evidence.filter(e=>h.evidence_ids.includes(e.id)).map(e=><article key={e.id}><p>{e.id} · {t('物理页','Physical page')} {e.page} · {e.chunk_id} [{e.normalized_start}, {e.normalized_end})</p><blockquote>{e.quote}</blockquote></article>)}<ul>{h.conditions.map((c,i)=><li key={i}>{c}</li>)}</ul></details>
  </article>;
}
export function IndustryMaterialPlanView({plan}:{plan:IndustryMaterialPlan}){
  const {t}=useI18n(),paperId=planText(plan.material.source.paper_id);
  return <section aria-label={t('准确固定计划来源','Exact fixed plan provenance')} data-material-plan-id={plan.plan_id}>
    <h3>{t('冻结计划及材料来源','Frozen plan and material provenance')}</h3><p><code>{plan.plan_id}</code></p>
    <p>{t('材料版本','Material version')}: <code>{plan.material.version_id}</code><br/>{t('材料摘要','Material digest')}: <code>{plan.material.version_digest}</code></p>
    <p>{t('论文身份','Paper identity')}: {paperId}<br/>PDF SHA256: <code>{planText(plan.material.source.paper_sha256)}</code></p>
    <p>{t('原始模型','Original model')}: {planText(plan.material.provider_origin.model)} · {t('提案','Proposal')}: {planText(plan.material.source.proposal_id)} · {t('模型尝试','Model attempt')}: {planText(plan.material.source.attempt_id)}</p>
    <p>{t('原始响应摘要','Original response digest')}: <code>{planText(plan.material.provider_origin.raw_response_sha256)}</code> · {t('语义独立评定','Independent semantic assessment')}: {t('未评定','Unassessed')}</p>
    <p><strong>{t('采纳主张','Adopted claim')}:</strong> {plan.material.hypothesis.claim} · {plan.material.hypothesis.claim_attribution}</p>
    <p>{t('审核声明','Review declaration')}: {plan.review.review_source} · {plan.review.reviewer_id}<br/>{plan.review.rationale}</p>
    <p>{t('固定计算：H−12..H−2 的十一项月度收益复合，跳过 H−1；完整历史、不填补。','Fixed computation: compound eleven monthly returns H−12..H−2; skip H−1; complete history and no fill.')}</p>
    <p>{t('行业来源','Industry source')}: <code>{plan.industry_source.id}</code> · {t('原论文个股复现保持阻断，保留期未评价。','The original stock reproduction remains blocked; the reserved period is unevaluated.')}</p>
    <h4>{t('原论文与项目改动','Paper and project modifications')}</h4><ul>{plan.candidate.changes.map((c,i)=><li key={i}>{c}</li>)}</ul>
    <details><summary>{t('引用和引用修订','Citations and citation revisions')}</summary>{plan.material.evidence.map(e=><article key={e.id}><p>{e.id} · {t('物理页','Physical page')} {e.page} · {e.chunk_id} [{e.normalized_start}, {e.normalized_end})</p><blockquote>{e.quote}</blockquote>
      {paperId!=='—'&&<a href={`/api/papers/${encodeURIComponent(paperId)}/pdf#page=${e.page}`} target="_blank" rel="noreferrer">{t('核对已校验论文原页','Check the verified original paper page')}</a>}</article>)}
      {!plan.material.changes.length?<p>{t('此采纳版本未记录修改；不猜测历史。','This adopted version records no changes; history is not inferred.')}</p>:plan.material.changes.map((c,i)=><article key={i}><h5>{t('采纳修订字段','Adoption change')} {c.path}</h5><details><summary>{t('查看原值与采纳值','Inspect original and adopted values')}</summary><p>{t('原值','Original')}</p><pre>{c.before_json}</pre><p>{t('采纳值','Adopted')}</p><pre>{c.after_json}</pre></details></article>)}
    </details><details><summary>{t('核对完整固定规则与公开来源身份','Inspect complete fixed rules and public source identities')}</summary><JsonDetails label={t('完整规则和冻结身份','Complete rules and frozen identities')} value={plan}/></details>
  </section>;
}
function SavedIndustryMaterialPlan({identity,workspaceScope,onOpenExperiment}:{identity:{id:string;digest:string};workspaceScope:WorkspaceRequestScope;onOpenExperiment:(id:string)=>void}){
  const {t}=useI18n(),[plan,setPlan]=useState<IndustryMaterialPlan|null>(null),[error,setError]=useState(''),[reload,setReload]=useState(0),[loading,setLoading]=useState(false),[busy,setBusy]=useState(false),[confirmed,setConfirmed]=useState(false),[child,setChild]=useState('');
  const mounted=useRef(false),sending=useRef(false);const mutation=useRecoverableMutation<PlanStarted>(recoveryKey(workspaceScope.workspaceId,'industry-material-plan-start',identity.id,identity.digest),`${industryMaterialPlanBase}/${encodeURIComponent(identity.id)}/start`);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController();setPlan(null);setChild('');setError('');setLoading(true);setConfirmed(false);
    void api<IndustryMaterialLineage>(materialPlanPath(identity.id,identity.digest).replace('?', '/lineage?'),{signal:abort.signal}).then(value=>{if(!workspaceScope.isCurrent(scope,abort.signal))return;assertMaterialPlan(value.plan,identity);setPlan(value.plan);setChild(value.lineage.experiment_id||'');})
      .catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal))setError(describeError(e));}).finally(()=>{if(workspaceScope.isCurrent(scope,abort.signal))setLoading(false);});return()=>abort.abort();
  },[identity.id,identity.digest,reload,workspaceScope]);
  async function start(body?:Record<string,unknown>){if(sending.current)return;sending.current=true;setBusy(true);setError('');const scope=workspaceScope.capture();
    try{const ack=await mutation.execute(body);if(!mounted.current||!workspaceScope.isCurrent(scope))return;if(ack.plan_id!==identity.id||ack.plan_digest!==identity.digest||!ack.experiment_id)throw Error(t('启动回执不属于此准确计划。','The start receipt belongs to another plan.'));
      setChild(ack.experiment_id);mutation.acknowledge(()=>setConfirmed(false));onOpenExperiment(ack.experiment_id);
    }catch(e){if(mounted.current&&workspaceScope.isCurrent(scope))setError(describeError(e));}finally{sending.current=false;if(mounted.current&&workspaceScope.isCurrent(scope))setBusy(false);}}
  return <section aria-label={t('已保存固定计划与执行','Saved fixed plan and execution')} aria-busy={loading}>
    {loading&&<p role="status">{t('正在核验保存的准确计划…','Verifying the exact saved plan…')}</p>}{error&&<p role="alert">{error}</p>}
    <button type="button" className="button small" disabled={busy} onClick={()=>setReload(v=>v+1)}>{t('重新读取此计划','Reload this plan')}</button>
    <MutationRecovery name={t('固定计划启动','fixed plan start')} pending={mutation.pending} error={mutation.error} busy={busy} onRetry={()=>void start()} onDismiss={mutation.dismissRejected}/>
    {plan&&<><IndustryMaterialPlanView plan={plan}/><fieldset disabled={busy||!mutation.ready||mutation.blocked||!!mutation.pending}>
      <label><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>{child?t('明确确认此计划的既有实验关联；不创建重复计算或模型调用','Explicitly confirm this plan’s existing experiment; no duplicate computation or model call'):t('明确启动此准确固定计划的计算；不调用模型','Explicitly start computation of this exact plan; no model call')}</label>
      <button type="button" className="button primary" disabled={!confirmed} onClick={()=>void start({expected_plan_id:identity.id,expected_plan_digest:identity.digest,confirm_start:true})}>{child?t('确认既有计划实验','Confirm the existing plan experiment'):t('执行此固定计划','Run this fixed plan')}</button>
    </fieldset>{child&&<button type="button" className="button" onClick={()=>onOpenExperiment(child)}>{t('查看此计划的实际实验','Open the actual experiment for this plan')}</button>}</>}
  </section>;
}
export function IndustryMomMaterialPlanOrigin({experimentId,target,workspaceScope}:{experimentId:string;target?:{attempt_id:string;result_digest:string}|null;workspaceScope:WorkspaceRequestScope}){
  const {t}=useI18n(),[activated,setActivated]=useState(false),[result,setResult]=useState<IndustryExperimentMaterialLineage|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(false),[reload,setReload]=useState(0),sequence=useRef(0);
  const targetKey=bodyKey(target),identity=`${experimentId}:${targetKey}`;
  useEffect(()=>{const scope=workspaceScope.capture(),abort=new AbortController(),epoch=++sequence.current;setResult(null);setError('');setLoading(false);if(!activated)return()=>abort.abort();setLoading(true);
    const params=target?`?${new URLSearchParams({expected_attempt_id:target.attempt_id,expected_result_digest:target.result_digest})}`:'';
    void api<IndustryExperimentMaterialLineage>(`/industry-mom-experiments/${encodeURIComponent(experimentId)}/material-lineage${params}`,{signal:abort.signal}).then(v=>{
      if(!workspaceScope.isCurrent(scope,abort.signal)||sequence.current!==epoch)return;assertExperimentMaterialLineage(v,experimentId,target);setResult(v);
    }).catch(e=>{if(workspaceScope.isCurrent(scope,abort.signal)&&sequence.current===epoch)setError(describeError(e));}).finally(()=>{if(workspaceScope.isCurrent(scope,abort.signal)&&sequence.current===epoch)setLoading(false);});return()=>abort.abort();
  },[activated,identity,reload,workspaceScope]);
  return <section aria-label={t('本次实验的材料计划来源','Material plan provenance for this experiment')} aria-busy={loading}>
    <details onToggle={event=>{if(event.currentTarget.open)setActivated(true);}}><summary>{t('从本次实验追溯材料与固定计划','Trace this experiment to its material and fixed plan')}</summary>
      <p>{t('只读核验，不调用模型，不修改原材料或创建实验。旧记录没有绑定时明确保留无关联。','Read-only verification calls no model and does not edit material or create an experiment. Unlinked historical records remain explicitly unlinked.')}</p>
      {loading&&<p role="status">{t('正在核验此实验的材料计划来源…','Verifying material-plan provenance for this experiment…')}</p>}{error&&<p role="alert">{error}</p>}
      {activated&&<button type="button" className="button small" onClick={()=>setReload(v=>v+1)}>{t('重新读取实验来源','Reload experiment provenance')}</button>}
      {result&&!result.linked&&<p role="status">{t('此实验未关联采纳材料计划；保留普通固定方法来源，不猜补模型历史。','This experiment has no adopted-material plan. Its ordinary fixed-method provenance is retained without inventing model history.')}</p>}
      {result?.items.map(item=><article key={item.plan.plan_id}><IndustryMaterialPlanView plan={item.plan}/><p>{t('关联实验','Linked experiment')}: {item.lineage.experiment_id} · {t('实际尝试','Actual attempt')}: {item.lineage.attempt_id||t('未开始','Not started')} · {item.lineage.status}</p><p>{t('实际结果摘要','Actual result digest')}: <code>{item.lineage.result_digest||t('未产生','Not produced')}</code></p>{item.lineage.error&&<p>{item.lineage.error}</p>}</article>)}
    </details>
  </section>;
}
