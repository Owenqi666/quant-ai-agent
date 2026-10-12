import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { FormulaAlignmentView } from "./components/FormulaAlignment";
import ReviewEvidence from "./components/ReviewEvidence";
import type { FormulaAlignmentResponse } from "./formulaAlignment";
import { describe,expect,it } from "vitest";
import type { Run } from "./domain";
import { assertFormulaAlignment,assertFormulaTarget,formulaAlignmentPath,formulaAlignmentTarget,formulaAlignmentTargetKey } from "./formulaAlignment";
const state="a".repeat(64),result="b".repeat(64);
const run:Run={id:"run",revision_id:"revision",research_id:"research",status:"completed",mode:"normalized_fixed",created_at:"",attempt_count:1,verification:{verified:true},regression_target:{attempt_id:"attempt",result_digest:state},review_targets:[{candidate_id:"c",attempt_id:"attempt",result_digest:result}],state:{candidates:[{id:"c",hypothesis_id:"h",expression:"close",origin:"user_modification",changes:[],status:"evaluated"}]}};
const target=formulaAlignmentTarget(run,"c")!;
describe("formula alignment exact target",()=>{
  it("derives one verified terminal target and refuses missing/duplicate/stale identities",()=>{
    expect(target).toEqual({run_id:"run",revision_id:"revision",candidate_id:"c",attempt_id:"attempt",state_digest:state,result_digest:result});
    for(const value of [{...run,status:"running"},{...run,verification:{verified:false}},{...run,review_targets:[...run.review_targets!,...run.review_targets!]},{...run,regression_target:{attempt_id:"other",result_digest:state}}])expect(formulaAlignmentTarget(value,"c")).toBeNull();
  });
  it("requests only the bounded candidate/state/result GET query",()=>{
    const url=new URL(formulaAlignmentPath(target),"http://localhost");expect(url.pathname).toBe("/runs/run/formula-alignment");expect(Object.fromEntries(url.searchParams)).toEqual({candidate_id:"c",expected_attempt_id:"attempt",expected_state_digest:state,expected_result_digest:result});
  });
  it("rejects all six wrong target fields and an unrelated payload kind",()=>{
    const value={schema_version:1,kind:"run_formula_alignment",target};expect(()=>assertFormulaTarget(value,target)).not.toThrow();
    for(const key of Object.keys(target) as (keyof typeof target)[]){const changed={...value,target:{...target,[key]:"different"}};expect(()=>assertFormulaTarget(changed,target)).toThrow();expect(formulaAlignmentTargetKey(changed.target)).not.toBe(formulaAlignmentTargetKey(target));}
    expect(()=>assertFormulaTarget({...value,kind:"run_evidence_lineage"},target)).toThrow();
  });
});

const responseFixture: FormulaAlignmentResponse = {
  "schema_version": 1,
  "kind": "run_formula_alignment",
  "target": {
    "run_id": "example-run",
    "revision_id": "example-revision",
    "candidate_id": "c",
    "attempt_id": "example-attempt",
    "state_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "result_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "policy_version": "bounded-formula-structure-v1-decimal-literals",
  "reference": {
    "status": "identified",
    "reason_code": "REFERENCE_UNIQUE_SUPPORTED",
    "mode": "standalone",
    "alpha_number": 101,
    "expression": "((close - open) / ((high - low) + .001))",
    "citations": [
      {
        "evidence_id": "e1",
        "page": 15
      }
    ]
  },
  "candidate": {
    "hypothesis_id": "h",
    "origin": "user_modification",
    "submitted_expression": "rank(((close - open) / ((high - low) + .001)))",
    "executed_expression": "rank(((close - open) / ((high - low) + .001)))",
    "declared_changes": [
      "Project rank wrapper; no original-paper claim."
    ]
  },
  "reference_to_declared": {
    "status": "different",
    "reason_code": "STRUCTURE_DIFFERENT"
  },
  "declared_to_executed": {
    "status": "exact",
    "reason_code": "STRUCTURE_EXACT"
  },
  "features": {
    "reference": {
      "fields": [
        "close",
        "high",
        "low",
        "open"
      ],
      "operators": [],
      "windows": [],
      "constants": [
        "decimal:0.001"
      ],
      "signature_digest": "db8bd436015206a102cfed521b7e680d11d6b76dd97ba1de7bdef354fc831b96"
    },
    "declared": {
      "fields": [
        "close",
        "high",
        "low",
        "open"
      ],
      "operators": [
        "rank"
      ],
      "windows": [],
      "constants": [
        "decimal:0.001"
      ],
      "signature_digest": "70ad420ecf4687fd4e5dea17e693a5b09d7c68d56fdc500a95cdc16b75ffdcdd"
    },
    "added_fields": [],
    "removed_fields": [],
    "added_operators": [
      "rank"
    ],
    "removed_operators": [],
    "window_changed": false,
    "constants_changed": false,
    "structure_changed": true,
    "outer_transform": "rank"
  },
  "field_declarations": {
    "used": [
      "close",
      "high",
      "low",
      "open"
    ],
    "required": [
      "close",
      "high",
      "low",
      "open",
      "volume"
    ],
    "undeclared": [],
    "declared_unused": [
      "volume"
    ]
  },
  "execution": {
    "status": "available",
    "operator_semantics_version": null,
    "split": null,
    "signal": null,
    "entry": null,
    "exit": null,
    "label": null,
    "weights": null,
    "rank_ic": null,
    "data_kind": null,
    "price_units": null,
    "volume_units": null
  },
  "checks": [
    {
      "dimension": "expression_legality",
      "status": "passed",
      "code": "DSL_VALID",
      "message": "Safe local DSL and actual available fields only; no economic judgment. Missing available fields do not erase a supported structural profile."
    },
    {
      "dimension": "formula_reference",
      "status": "passed",
      "code": "REFERENCE_UNIQUE_SUPPORTED",
      "message": "Only uniquely supported cited formula syntax is compared."
    },
    {
      "dimension": "field_declarations",
      "status": "warning",
      "code": "DECLARED_UNUSED_FIELDS",
      "message": "Used fields must be declared; unused declarations are hints, not proof of a research omission."
    },
    {
      "dimension": "execution_identity",
      "status": "passed",
      "code": "STRUCTURE_EXACT",
      "message": "Declared-to-executed comparison is independent of paper-reference comparison."
    },
    {
      "dimension": "attribution_declarations",
      "status": "passed",
      "code": "ATTRIBUTION_DECLARED",
      "message": "Origin/change declarations only; no automatic claim or human approval."
    }
  ],
  "unassessed_semantics": [
    "economic_mechanism",
    "natural_language_direction",
    "field_units",
    "calendar_and_missing_policy",
    "algebraic_equivalence",
    "overall_hypothesis_fidelity"
  ],
  "semantic_fidelity": "unverified",
  "semantic_quality_score": null,
  "human_review_written": false,
  "llm_api_called": false,
  "public_projection_redacted": false,
  "public_view_digest": "30b5d2a3b27da70e3f1e335d05ab221b414b7de178e266ea11349dc10e14ee45"
};
const response = () => ({...structuredClone(responseFixture), target});
const sourceRun: Run = {
  ...run,
  state: {task: {
    candidates: [{id:"c",hypothesis_id:"h",expression:responseFixture.candidate.submitted_expression,origin:"user_modification",changes:responseFixture.candidate.declared_changes}],
    hypotheses: [{id:"h",claim:"Alpha#101",attribution:"paper_original",evidence_ids:["e1"],economic_mechanism:"unverified explanation",mechanism_attribution:"model_conjecture",signal_direction:"higher",required_fields:responseFixture.field_declarations.required,assumptions:[]}],
    evidence:[{id:"e1",page:15,quote:responseFixture.reference.expression!}]
  }, candidates:[{id:"c",hypothesis_id:"h",expression:responseFixture.candidate.submitted_expression,origin:"user_modification",changes:responseFixture.candidate.declared_changes,status:"evaluated",result:{expression:responseFixture.candidate.executed_expression!,metrics:{mean_rank_ic:0.2,mean_gross_return:0.001,evaluated_days:4}}}]}
};
const html = (value:FormulaAlignmentResponse) => renderToStaticMarkup(createElement(FormulaAlignmentView,{alignment:value}));
describe("frozen structural comparison scope",()=>{
  it("keeps different paper-to-declaration and exact declaration-to-execution independent",()=>{
    const value=response();expect(()=>assertFormulaAlignment(value,target,sourceRun)).not.toThrow();
    const rendered=html(value);expect(rendered).toContain('data-formula-comparison="reference_to_declared" data-comparison-status="different"');expect(rendered).toContain('data-formula-comparison="declared_to_executed" data-comparison-status="exact"');expect(rendered).toContain("横截面 rank");expect(rendered).toContain("相同指标");expect(rendered).toContain("语义仍未评审");expect(rendered).toContain("不等同人工五维");expect(rendered).not.toContain("语义审核通过");
  });
  it("missing reference never erases valid declared-to-executed facts or existing metrics",()=>{
    const value=response();value.reference={status:"unavailable",reason_code:"REFERENCE_NOT_FOUND",mode:"none",alpha_number:null,expression:null,citations:[]};value.reference_to_declared={status:"unknown",reason_code:"REFERENCE_NOT_FOUND"};value.features={...value.features,reference:null,added_fields:[],removed_fields:[],added_operators:[],removed_operators:[],window_changed:null,constants_changed:null,structure_changed:null,outer_transform:null};
    expect(()=>assertFormulaAlignment(value,target,sourceRun)).not.toThrow();expect(html(value)).toContain("不可比较");expect(html(value)).toContain("有限策略下结构一致");
    const rendered=renderToStaticMarkup(createElement(ReviewEvidence,{run:sourceRun,candidateId:"c",artifacts:[]}));expect(rendered).toContain("候选计算指标");expect(rendered).toContain("0.200000");
  });
  it("refuses wrong loaded run and any mismatch in frozen declarations or cited source",()=>{
    for(const mutate of [
      (v:FormulaAlignmentResponse)=>v.candidate.hypothesis_id="other",
      (v:FormulaAlignmentResponse)=>v.candidate.origin="paper_original",
      (v:FormulaAlignmentResponse)=>v.candidate.submitted_expression="close",
      (v:FormulaAlignmentResponse)=>v.candidate.executed_expression="open",
      (v:FormulaAlignmentResponse)=>v.candidate.declared_changes=[],
      (v:FormulaAlignmentResponse)=>v.reference.citations[0].page=4,
      (v:FormulaAlignmentResponse)=>v.reference.citations[0].evidence_id="unrelated",
      (v:FormulaAlignmentResponse)=>v.reference.citations.push({...v.reference.citations[0]})
    ]){const value=response();mutate(value);expect(()=>assertFormulaAlignment(value,target,sourceRun)).toThrow();}
    const duplicated=structuredClone(sourceRun);duplicated.state!.task!.candidates!.push({...duplicated.state!.task!.candidates![0]});expect(()=>assertFormulaAlignment(response(),target,duplicated)).toThrow();
    expect(()=>assertFormulaAlignment(response(),target,{...sourceRun,id:"other-run"})).toThrow();
  });
  it("accepts public redaction without interpreting displayed expressions as private source",()=>{
    const value=response();value.public_projection_redacted=true;value.candidate.submitted_expression="[redacted]";value.candidate.executed_expression="[redacted]";value.candidate.declared_changes=["[redacted]"];expect(()=>assertFormulaAlignment(value,target,sourceRun)).not.toThrow();expect(html(value)).toContain("公开投影已脱敏");value.reference.citations[0].page=4;expect(()=>assertFormulaAlignment(value,target,sourceRun)).toThrow();
  });
  it("refuses semantic approval, human/model flags, incomplete scopes and invalid windows",()=>{
    for(const mutate of [
      (v:FormulaAlignmentResponse)=>(v as unknown as Record<string,unknown>).semantic_fidelity="verified",
      (v:FormulaAlignmentResponse)=>(v as unknown as Record<string,unknown>).human_review_written=true,
      (v:FormulaAlignmentResponse)=>(v as unknown as Record<string,unknown>).llm_api_called=true,
      (v:FormulaAlignmentResponse)=>(v as unknown as Record<string,unknown>).semantic_quality_score=1,
      (v:FormulaAlignmentResponse)=>v.unassessed_semantics.pop(),
      (v:FormulaAlignmentResponse)=>v.unassessed_semantics[0]=v.unassessed_semantics[1],
      (v:FormulaAlignmentResponse)=>v.checks.pop(),
      (v:FormulaAlignmentResponse)=>v.features.declared!.windows.push({operator:"ts_sum",rows:253}),
      (v:FormulaAlignmentResponse)=>v.features.structure_changed=false,
      (v:FormulaAlignmentResponse)=>v.reference.status="ambiguous"
    ]){const value=response();mutate(value);expect(()=>assertFormulaAlignment(value,target,sourceRun)).toThrow();}
  });
  it("shows row windows, fields and structural negation without inventing economic direction",()=>{
    const value=response();value.features.outer_transform="negation";value.features.declared!.windows=[{operator:"ts_sum",rows:20}];value.features.window_changed=true;value.features.added_fields=["volume"];value.field_declarations.undeclared=["volume"];
    const rendered=html(value);expect(rendered).toContain("ts_sum: 20 行");expect(rendered).toContain("区别于训练/验证/测试日期区间");expect(rendered).toContain("不自动验证文字信号方向");expect(rendered).toContain("未声明使用字段：");expect(rendered).toContain("volume");expect(rendered).toContain("整体假说忠实度");
  });
  it("renders all public strings as text instead of active markup",()=>{
    const value=response();value.reference.expression='<img src=x onerror="alert(1)">';value.candidate.declared_changes=['<script>alert(1)</script>'];value.checks[0].message="<b>untrusted</b>";const rendered=html(value);expect(rendered).toContain("&lt;img");expect(rendered).toContain("&lt;script&gt;");expect(rendered).not.toContain("<img");expect(rendered).not.toContain("<script>");
  });
  it("initial disclosure is collapsed and never claims completed semantic review",()=>{
    const rendered=renderToStaticMarkup(createElement(ReviewEvidence,{run:sourceRun,candidateId:"c",artifacts:[]}));expect(rendered).toContain("公式结构与声明差异");expect(rendered).toContain("展开后读取本次候选，不调用模型");expect(rendered).not.toContain('data-formula-comparison=');
  });
});
