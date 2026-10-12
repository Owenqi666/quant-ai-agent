import type {AcceptedMaterialDraft, MonthlyMomReview, IndustryMomMaterialPreview, IndustryMomMaterialPlan, IndustryMomMaterialPlanSummary, IndustryMomMaterialPlanPage, IndustryMomMaterialLineage, IndustryMomExperimentMaterialLineage} from './generated/api-contract';

export const industryMaterialPlanBase = '/industry-mom-material-plans';
export const monthlyReviewChecks = [
  'citation_accuracy_checked', 'hypothesis_fidelity_checked', 'monthly_return_semantics_checked',
  'scope_modifications_checked', 'no_unsupported_field_substitution',
] as const;
export type MonthlyReviewCheck = typeof monthlyReviewChecks[number];
export type MonthlyReviewDraft = {reviewSource: ''|'user'|'codex_proxy'; reviewer: string; rationale: string;
  hypothesisId: string; evidenceIds: string[]; checks: Record<MonthlyReviewCheck, boolean>};
export const emptyMonthlyReview = (): MonthlyReviewDraft => ({reviewSource:'',reviewer:'',rationale:'',hypothesisId:'',evidenceIds:[],
  checks:Object.fromEntries(monthlyReviewChecks.map(key=>[key,false])) as Record<MonthlyReviewCheck,boolean>});
export type MonthlyReview = MonthlyMomReview;
export type MaterialPlanRequest = Omit<IndustryMomMaterialPreview,'idempotency_key'>;
export type IndustryMaterialPlan = IndustryMomMaterialPlan;
export type IndustryMaterialPlanSummary = IndustryMomMaterialPlanSummary;
export type IndustryMaterialPlanPage = IndustryMomMaterialPlanPage;
export type IndustryMaterialLineage = IndustryMomMaterialLineage;
export type IndustryExperimentMaterialLineage = IndustryMomExperimentMaterialLineage;
export function materialRequestKey(value:unknown):string {
  const stable=(input:unknown):unknown=>Array.isArray(input)?input.map(stable):input&&typeof input==='object'?Object.fromEntries(Object.entries(input).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([k,v])=>[k,stable(v)])):input;
  return JSON.stringify(stable(value));
}
export async function materialPlanJsonDigest(value:unknown):Promise<string> {
  const sorted=JSON.parse(materialRequestKey(value));
  const bytes=new TextEncoder().encode(JSON.stringify(sorted,null,2)+"\n");
  const hash=await crypto.subtle.digest('SHA-256',bytes);
  return Array.from(new Uint8Array(hash),byte=>byte.toString(16).padStart(2,'0')).join('');
}
export const monthlyReviewDigest=(review:MonthlyReview)=>materialPlanJsonDigest(review);
export const materialPlanPath = (id:string,digest:string) => `${industryMaterialPlanBase}/${encodeURIComponent(id)}?${new URLSearchParams({expected_plan_digest:digest})}`;
export function monthlyReviewBody(draft:MonthlyReviewDraft,selection:{materialId:string;versionId:string;materialDigest:string;sourceId:string;sourceDigest:string;
  material:AcceptedMaterialDraft}):MaterialPlanRequest {
  if(!draft.reviewSource||!draft.reviewer.trim()||!draft.rationale.trim()||!monthlyReviewChecks.every(key=>draft.checks[key]))
    throw Error('请明确填写审核来源、身份、理由并分别核对五项审核事项。');
  const hypothesis=selection.material.hypotheses.find(item=>item.id===draft.hypothesisId);
  if(!hypothesis||hypothesis.unresolved_rules.length||selection.material.unresolved_rules.length)throw Error('所选材料版本或假说存在未决规则，不能建立固定计划。');
  const allowed=new Set(hypothesis.evidence_ids);
  if(!draft.evidenceIds.length||new Set(draft.evidenceIds).size!==draft.evidenceIds.length||draft.evidenceIds.some(id=>!allowed.has(id)))
    throw Error('请明确选择本假说引用的证据，不得借用其他假说的引文。');
  if(!selection.materialId||!selection.versionId||!selection.materialDigest||!selection.sourceId||!selection.sourceDigest)throw Error('缺少完整材料版本或行业来源。');
  return {source_id:selection.sourceId,source_digest:selection.sourceDigest,review:{schema_version:1,review_source:draft.reviewSource,reviewer_id:draft.reviewer.trim(),
    decision:'approved_for_fixed_industry_demo',material_id:selection.materialId,version_id:selection.versionId,material_digest:selection.materialDigest,
    hypothesis_id:draft.hypothesisId,evidence_ids:[...draft.evidenceIds],citation_accuracy_checked:true,hypothesis_fidelity_checked:true,
    monthly_return_semantics_checked:true,scope_modifications_checked:true,no_unsupported_field_substitution:true,window_months:11,skip_months:1,
    missing_policy:'complete',fill_policy:'none',rationale:draft.rationale.trim()}};
}
export function assertMaterialPlan(plan:IndustryMaterialPlan,expected?:{id:string;digest:string},request?:MaterialPlanRequest,reviewDigest?:string):void {
  if(!plan||!/^industry_mom_plan_[a-f0-9]{64}$/.test(plan.plan_id)||!/^[a-f0-9]{64}$/.test(plan.plan_digest)
    ||plan.plan_id!==`industry_mom_plan_${plan.plan_digest}`||plan.reserved_evaluated!==false||plan.original_single_stock_plan_unlocked!==false
    ||plan.model_generated_monthly_dsl!==false||plan.human_satisfaction_measured!==false||plan.material.semantic_fidelity!=='unverified'
    ||plan.review.material_id!==plan.material.id||plan.review.version_id!==plan.material.version_id||plan.review.material_digest!==plan.material.version_digest)
    throw Error('固定计划的材料身份、摘要或研究范围不匹配。');
  if(expected&&(plan.plan_id!==expected.id||plan.plan_digest!==expected.digest))throw Error('固定计划不属于选中的准确版本。');
  if(request&&(!reviewDigest||plan.review_digest!==reviewDigest||plan.industry_source.id!==request.source_id||plan.industry_source.digest!==request.source_digest))
    throw Error('预览不属于本次明确审核或行业来源。');
}
export function assertExperimentMaterialLineage(value:IndustryExperimentMaterialLineage,experimentId:string,target?:{attempt_id:string;result_digest:string}|null):void {
  if(value.experiment_id!==experimentId||value.linked!==!!value.items.length)throw Error('材料计划关联不属于本次行业实验。');
  for(const item of value.items){assertMaterialPlan(item.plan);if(item.lineage.experiment_id!==experimentId
    ||(target&&(item.lineage.attempt_id!==target.attempt_id||item.lineage.result_digest!==target.result_digest)))throw Error('材料计划关联不属于本次准确尝试或结果。');}
}
