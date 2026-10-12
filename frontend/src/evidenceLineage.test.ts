import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Run } from "./domain";
import { assertLineage, lineagePath, lineageTarget, lineageTargetKey, normalizeLineageQuote } from "./evidenceLineage";
import type { EvidenceLineageResponse, LineageQuote } from "./evidenceLineage";
import { EvidenceLineageView } from "./components/EvidenceLineage";
import ReviewEvidence from "./components/ReviewEvidence";

const sha="a".repeat(64), state="b".repeat(64), result="c".repeat(64);
const hypothesis={id:"h",claim:"frozen claim",attribution:"paper_original" as const,evidence_ids:["e"],economic_mechanism:"mechanism",mechanism_attribution:"model_conjecture" as const,signal_direction:"higher",required_fields:["close"],assumptions:[]};
const run:Run={id:"run",revision_id:"revision",research_id:"research",status:"completed",mode:"normalized_fixed",created_at:"",attempt_count:1,verification:{verified:true},regression_target:{attempt_id:"attempt",result_digest:state},review_targets:[{candidate_id:"c",attempt_id:"attempt",result_digest:result}],state:{task:{candidates:[{id:"c",hypothesis_id:"h",expression:"close",origin:"paper_original",changes:[]}],hypotheses:[hypothesis],evidence:[{id:"e",page:15,quote:"😀原文"}]},candidates:[{id:"c",hypothesis_id:"h",expression:"close",origin:"paper_original",changes:[],status:"evaluated",result:{metrics:{mean_rank_ic:0,evaluated_days:3}}}]}};
const target=lineageTarget(run,"c")!;
const quote:LineageQuote={id:"e",page:15,quote:"😀原文",chunk_id:"page-15-chars-0-10",normalized_start:1,normalized_end:4,printed_page:null,original_quote_sha256:sha};
const materialHypothesis={id:"h",claim:"frozen claim",claim_attribution:"paper_original" as const,evidence_ids:["e"],economic_mechanism:"mechanism",mechanism_attribution:"model_conjecture" as const,signal_direction:"higher",required_data:[],calculation:"close",conditions:[],unresolved_rules:[]};
const proposal={proposal_id:"p",attempt_id:"pa",context_digest:sha,accepted_response_digest:sha};
const valid:EvidenceLineageResponse={schema_version:1,kind:"run_evidence_lineage",target,status:"verified",reason_code:"RECORDED_MATERIAL_CHAIN",provenance:{experiment_id:"experiment",generation:proposal,extraction:{...proposal,proposal_id:"extraction"},material:{id:"material",version_id:"version-one",draft_digest:sha,material_digest:sha},selection:{paper_id:"paper",paper_sha256:sha,document_digest:sha,selection_digest:sha,normalization_version:"nfkc-whitespace-unicode-v1",offset_basis:"normalized_pdf_page_unicode_code_points_half_open",printed_page_verification:"unverified"}},hypothesis:{id:"h",status:"unchanged",inheritance_intact:true,original:materialHypothesis,adopted:materialHypothesis,final:hypothesis,changes:[]},evidence:[{id:"e",status:"unchanged",reason_code:"UNCHANGED_EXTRACTION",inheritance_intact:true,original:quote,adopted:quote,final:{id:"e",page:15,quote:"😀原文"},excerpt:{source_stage:"adopted",before:"前",quote:"😀原文",after:"后",bounded_context:true,exact_highlight:true,public_projection_redacted:false}}],history:[{version_id:"version-one",digest:sha,changed_paths:[]}],semantic_fidelity:"unverified",llm_api_called:false,public_projection_redacted:false,public_view_digest:sha};
const copy=()=>structuredClone(valid);
const html=(value:EvidenceLineageResponse)=>renderToStaticMarkup(createElement(EvidenceLineageView,{lineage:value}));

describe("frozen evidence lineage identity and interpretation",()=>{
  it("requires unique verified candidate and a canonical state/candidate digest",()=>{
    expect(lineageTarget(run,"c")).toEqual(target);
    for(const change of [{...run,status:"running"},{...run,verification:{verified:false}},{...run,review_targets:[...run.review_targets!,...run.review_targets!]},{...run,regression_target:{attempt_id:"other",result_digest:state}},{...run,regression_target:{attempt_id:"attempt",result_digest:"invalid"}},{...run,state:{...run.state,candidates:[...run.state!.candidates!,...run.state!.candidates!]}}])expect(lineageTarget(change,"c")).toBeNull();
    const url=new URL(lineagePath(target),"http://local");
    expect(url.pathname).toBe("/runs/run/evidence-lineage");
    expect(Object.fromEntries(url.searchParams)).toEqual({candidate_id:"c",expected_attempt_id:"attempt",expected_state_digest:state,expected_result_digest:result});
  });
  it("rejects each wrong run/revision/candidate/attempt/state/result identity",()=>{
    expect(()=>assertLineage(valid,target,run)).not.toThrow();
    for(const key of Object.keys(target) as (keyof typeof target)[]) {const value=copy();value.target[key]="different";expect(()=>assertLineage(value,target,run)).toThrow("LINEAGE_RESPONSE_MISMATCH");expect(lineageTargetKey(value.target)).not.toBe(lineageTargetKey(target));}
  });
  it("rejects duplicate/missing evidence IDs and conflicting declared hypothesis",()=>{
    const duplicate=copy();duplicate.evidence.push(structuredClone(duplicate.evidence[0]));
    const missing=copy();missing.evidence=[];
    const wrong=copy();wrong.evidence[0].final.page=4;
    const changed=structuredClone(run);changed.state!.task!.candidates![0].hypothesis_id="other";
    for(const value of [duplicate,missing,wrong])expect(()=>assertLineage(value,target,run)).toThrow();
    expect(()=>assertLineage(valid,target,changed)).toThrow();
  });
  it("keeps quote unchanged separately from edited hypothesis and exact adopted version",()=>{
    const value=copy();value.hypothesis.status="edited";value.hypothesis.adopted!.claim="project change";value.hypothesis.changes=["/claim"];
    expect(()=>assertLineage(value,target,run)).not.toThrow();
    const rendered=html(value);expect(rendered).toContain("假说 h · 已编辑");expect(rendered).toContain("证据 e · 未改动");expect(rendered).toContain("version-one");expect(rendered).not.toContain("latest");expect(rendered).toContain("/claim");
  });
  it("does not infer continuous AI inheritance for same ID after removal and recreation",()=>{
    const value=copy();value.evidence[0].status="added";value.evidence[0].inheritance_intact=false;
    expect(()=>assertLineage(value,target,run)).not.toThrow();
    expect(html(value)).toContain("未证明连续继承");
    value.evidence[0].status="unchanged";expect(()=>assertLineage(value,target,run)).toThrow();
  });
  it("renders server split Unicode excerpt without JS offset slicing or HTML interpretation",()=>{
    const value=copy();value.evidence[0].excerpt!.quote="😀<script>alert(1)</script>原文";
    const rendered=html(value);expect(rendered).toContain("<mark>😀&lt;script&gt;alert(1)&lt;/script&gt;原文</mark>");expect(rendered).not.toContain("<script>");expect(rendered).toContain("[1, 4)");expect(rendered).toContain("不是 PDF 视觉坐标");expect(rendered).toContain("采用片段");
  });
  it("allows public redaction without comparing publicly changed quote to private source",()=>{
    const value=copy();value.public_projection_redacted=true;value.evidence[0].final.quote="[redacted source]";value.evidence[0].excerpt!.public_projection_redacted=true;value.evidence[0].excerpt!.exact_highlight=false;
    expect(()=>assertLineage(value,target,run)).not.toThrow();expect(html(value)).not.toContain("<mark>");expect(html(value)).toContain("已脱敏");
    value.evidence[0].excerpt!.exact_highlight=true;expect(()=>assertLineage(value,target,run)).toThrow();
    const globalOnly=copy();globalOnly.public_projection_redacted=true;expect(()=>assertLineage(globalOnly,target,run)).not.toThrow();expect(html(globalOnly)).toContain("<mark>");
  });
  it("matches Python NFKC whitespace including NEL and information separators",()=>{
    for (const separator of ["\u0085","\u001c","\u001d","\u001e","\u001f"]) {
      expect(normalizeLineageQuote(" "+"Ａ"+separator+"B ")).toBe("A B");
      const value=copy(),loaded=structuredClone(run);
      value.evidence[0].adopted!.quote="Ａ"+separator+"B";value.evidence[0].excerpt!.quote="A B";
      value.evidence[0].final.quote="Ａ"+separator+"B";loaded.state!.task!.evidence![0].quote="A B";
      expect(()=>assertLineage(value,target,loaded)).not.toThrow();
    }
  });
  it("preserves FEFF for both final quotation and adopted exact highlight",()=>{
    expect(normalizeLineageQuote("\ufeffA\ufeffB\ufeff")).toBe("\ufeffA\ufeffB\ufeff");
    const value=copy(),loaded=structuredClone(run);
    value.evidence[0].adopted!.quote="Ａ\ufeffB";value.evidence[0].excerpt!.quote="A\ufeffB";value.evidence[0].final.quote="Ａ\ufeffB";loaded.state!.task!.evidence![0].quote="A\ufeffB";
    expect(()=>assertLineage(value,target,loaded)).not.toThrow();
    value.evidence[0].excerpt!.quote="AB";expect(()=>assertLineage(value,target,loaded)).toThrow();
    value.evidence[0].excerpt!.quote="A\ufeffB";value.evidence[0].final.quote="AB";expect(()=>assertLineage(value,target,loaded)).toThrow();
  });
  it("rejects same-target exact highlight if the excerpt differs from adopted text",()=>{
    const value=copy();value.evidence[0].excerpt!.quote="unrelated quotation";expect(()=>assertLineage(value,target,run)).toThrow();
  });
  it("rejects malformed span, unsupported offset basis or inferred semantic approval",()=>{
    for(const mutate of [(v:EvidenceLineageResponse)=>v.evidence[0].adopted!.normalized_end=0,(v:EvidenceLineageResponse)=>(v.provenance!.selection as unknown as Record<string,unknown>).offset_basis="pdf_pixels",(v:EvidenceLineageResponse)=>(v as unknown as Record<string,unknown>).semantic_fidelity="verified"]){const value=copy();mutate(value);expect(()=>assertLineage(value,target,run)).toThrow();}
  });
  it("keeps manual source unknown and independent computation metrics visible",()=>{
    const value=copy();value.status="unknown";value.reason_code="MATERIAL_LINEAGE_NOT_RECORDED";value.provenance=null;value.hypothesis={...value.hypothesis,status:"unknown",inheritance_intact:false,original:null,adopted:null};value.history=[];value.evidence=value.evidence.map(item=>({...item,status:"unknown",inheritance_intact:false,original:null,adopted:null,excerpt:null}));
    expect(()=>assertLineage(value,target,run)).not.toThrow();expect(html(value)).toContain("保留为未知");expect(html(value)).not.toContain("<mark>");
    const rendered=renderToStaticMarkup(createElement(ReviewEvidence,{run,candidateId:"c",artifacts:[]}));expect(rendered).toContain("候选计算指标");expect(rendered).toContain("0.000000");
    value.evidence[0].original=quote;expect(()=>assertLineage(value,target,run)).toThrow();
  });
});
