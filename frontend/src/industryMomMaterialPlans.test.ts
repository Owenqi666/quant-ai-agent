import {describe,it,expect} from 'vitest';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {acceptedMaterial,hashes,ids,planFixture,reviewRequest} from '../e2e/industry-material-plan-fixtures';
import {assertMaterialPlan,assertExperimentMaterialLineage,emptyMonthlyReview,materialPlanJsonDigest,materialRequestKey,monthlyReviewBody,monthlyReviewChecks,monthlyReviewDigest} from './industryMomMaterialPlans';
import type {IndustryMaterialPlan,MonthlyReviewDraft} from './industryMomMaterialPlans';
import {IndustryMaterialPlanView} from './components/IndustryMomMaterialPlans';
const selection={materialId:ids.material,versionId:ids.version,materialDigest:hashes.b,sourceId:ids.source,sourceDigest:hashes.a,material:acceptedMaterial};
function completeDraft():MonthlyReviewDraft{const d=emptyMonthlyReview();Object.assign(d,{reviewSource:'codex_proxy',reviewer:'fixture-reviewer',rationale:'Synthetic proxy review; not a human evaluation.',hypothesisId:'momentum',evidenceIds:['window']});for(const key of monthlyReviewChecks)d.checks[key]=true;return d;}

describe('fixed monthly material plan boundaries',()=>{
  it('starts with no reviewer, source declaration, selected evidence or implied approval',()=>{
    const d=emptyMonthlyReview();expect(d.reviewSource).toBe('');expect(d.reviewer).toBe('');expect(d.rationale).toBe('');expect(d.evidenceIds).toEqual([]);expect(Object.values(d.checks)).toEqual([false,false,false,false,false]);expect(()=>monthlyReviewBody(d,selection)).toThrow();
  });
  it('requires every check independently and keeps proxy declarations distinct from human labels',()=>{
    for(const key of monthlyReviewChecks){const d=completeDraft();d.checks[key]=false;expect(()=>monthlyReviewBody(d,selection)).toThrow();}
    expect(monthlyReviewBody(completeDraft(),selection).review.review_source).toBe('codex_proxy');
  });
  it('refuses unresolved material/hypotheses, unselected or foreign citations and missing identities',()=>{
    for(const d of [{...completeDraft(),evidenceIds:[]},{...completeDraft(),evidenceIds:['other']},{...completeDraft(),evidenceIds:['window','window']},{...completeDraft(),hypothesisId:'other'}])expect(()=>monthlyReviewBody(d,selection)).toThrow();
    expect(()=>monthlyReviewBody(completeDraft(),{...selection,sourceId:''})).toThrow();
    expect(()=>monthlyReviewBody(completeDraft(),{...selection,material:{...acceptedMaterial,unresolved_rules:['unknown']}})).toThrow();
    expect(()=>monthlyReviewBody(completeDraft(),{...selection,material:{...acceptedMaterial,hypotheses:[{...acceptedMaterial.hypotheses[0],unresolved_rules:['unknown']}]}})).toThrow();
  });
  it('uses Python storage.digest bytes for nested object order, Unicode, arrays and newline',async()=>{
    const value={z:{beta:['研究😀',true,11],alpha:{b:'line\nsecond',a:false}},a:'代理'};
    expect(await materialPlanJsonDigest(value)).toBe('77e2526de2ea459abb61548a0dcc0d278de986962efbefc9a39e127725bb1585');
    expect(await materialPlanJsonDigest({a:'代理',z:{alpha:{a:false,b:'line\nsecond'},beta:['研究😀',true,11]}})).toBe(await materialPlanJsonDigest(value));
    const review=reviewRequest().review,reversed=Object.fromEntries(Object.entries(review).reverse());expect(await materialPlanJsonDigest(reversed)).toBe(await monthlyReviewDigest(review));
  });
  it('binds source, version and exact review digest even when text is redacted',async()=>{
    const request=reviewRequest(),plan=planFixture(request),reviewDigest=await monthlyReviewDigest(request.review);
    expect(()=>assertMaterialPlan(plan,undefined,request,reviewDigest)).not.toThrow();
    const redacted={...plan,public_projection_redacted:true,review:{...plan.review,reviewer_id:'[redacted]',rationale:'[redacted]'}};
    expect(()=>assertMaterialPlan(redacted,undefined,request,reviewDigest)).not.toThrow();
    expect(()=>assertMaterialPlan({...redacted,review_digest:hashes.a},undefined,request,reviewDigest)).toThrow();
    expect(()=>assertMaterialPlan(plan,undefined,{...request,source_digest:hashes.b},reviewDigest)).toThrow();
    expect(()=>assertMaterialPlan({...plan,material:{...plan.material,version_id:'other'}})).toThrow();
  });
  it('refuses another selected plan or unsafe research scope rather than borrowing a result',()=>{
    const plan=planFixture();expect(()=>assertMaterialPlan(plan,{id:'other',digest:hashes.c})).toThrow();
    for(const fields of [{reserved_evaluated:true},{model_generated_monthly_dsl:true},{original_single_stock_plan_unlocked:true},{human_satisfaction_measured:true}])expect(()=>assertMaterialPlan({...plan,...fields} as IndustryMaterialPlan)).toThrow();
  });
  it('accepts an explicit unlinked legacy record and rejects cross-experiment or stale attempt lineage',()=>{
    expect(()=>assertExperimentMaterialLineage({experiment_id:ids.experiment,linked:false,items:[]},ids.experiment)).not.toThrow();
    const linked={experiment_id:ids.experiment,linked:true,items:[{plan:planFixture(),lineage:{experiment_id:ids.experiment,status:'completed' as const,attempt_id:ids.attempt,result_digest:hashes.a,report_sha256:hashes.a,verification:null,error:null}}]};
    expect(()=>assertExperimentMaterialLineage(linked,ids.experiment,{attempt_id:ids.attempt,result_digest:hashes.a})).not.toThrow();
    expect(()=>assertExperimentMaterialLineage(linked,'other')).toThrow();expect(()=>assertExperimentMaterialLineage(linked,ids.experiment,{attempt_id:'old',result_digest:hashes.a})).toThrow();
  });
  it('shows readable material/model/review/citations/modifications with literal quotes retained',()=>{
    const html=renderToStaticMarkup(createElement(IndustryMaterialPlanView,{plan:planFixture()}));
    for(const text of ['fixture-model','fixture-reviewer','原始模型','冻结计划及材料来源','引用和引用修订',ids.version,'Individual stocks replaced',acceptedMaterial.evidence[0].quote])expect(html).toContain(text);
    expect(html).toContain('未评定');expect(html).toContain('gross_only_no_cost_model');expect(materialRequestKey({b:1,a:2})).toBe(materialRequestKey({a:2,b:1}));
  });
});
