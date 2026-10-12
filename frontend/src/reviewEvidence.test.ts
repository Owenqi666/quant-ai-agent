import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Artifact, Run } from "./domain";
import { observedMetric, reviewEvidence } from "./reviewEvidence";
import ReviewEvidence from "./components/ReviewEvidence";
import { CandidateComparison } from "./components/ResearchReviewForm";

const pdfSha = "a".repeat(64), stateDigest = "b".repeat(64), candidateDigest = "d".repeat(64);
const run: Run = {
  id:"run", research_id:"research", revision_id:"old-revision", status:"completed", mode:"normalized_fixed", created_at:"", attempt_count:2,
  verification:{verified:true}, review_targets:[{candidate_id:"alpha",attempt_id:"attempt-2",result_digest:candidateDigest}],
  regression_target:{attempt_id:"attempt-2",result_digest:stateDigest},
  state:{preflight:{evidence:[{id:"quote",page:15,quote:"frozen original quote",paper_id:"frozen-paper",document_sha256:pdfSha,citation_verified:true,semantic_fidelity:"requires_human_review"}]},task:{candidates:[{id:"alpha",hypothesis_id:"hypothesis",expression:"declared_close",origin:"user_modification",changes:[]}],
    hypotheses:[{id:"hypothesis",claim:"frozen claim",attribution:"user_modification",economic_mechanism:"conjecture",mechanism_attribution:"model_conjecture",signal_direction:"higher long",required_fields:["close"],assumptions:[],evidence_ids:["quote"]}],
    evidence:[{id:"quote",page:15,quote:"frozen original quote"}]},
    candidates:[{id:"alpha",hypothesis_id:"hypothesis",expression:"close",origin:"user_modification",changes:[],status:"evaluated",artifacts:{"candidates/alpha/attempt-1/factor.csv":"sha"},
      result:{expression:"executed_close",metrics:{mean_rank_ic:0,mean_gross_return:-0.001,factor_coverage:0.5,evaluated_days:4},market_metadata:{data_kind:"synthetic",version:"fixture"}}}]},
};
const files: Artifact[] = [
  {id:"pdf",attempt_id:"attempt-2",name:"inputs/paper.pdf",sha256:pdfSha,size:100},
  {id:"current",attempt_id:"attempt-2",name:"candidates/alpha/attempt-1/factor.csv",sha256:"sha",size:10},
  {id:"old",attempt_id:"attempt-1",name:"candidates/alpha/attempt-1/factor.csv",sha256:"sha",size:10},
  {id:"other",attempt_id:"attempt-2",name:"candidates/beta/attempt-1/factor.csv",sha256:"sha",size:10},
  {id:"report",attempt_id:"attempt-2",name:"report.md",sha256:"report-sha",size:10},
  {id:"task",attempt_id:"attempt-2",name:"inputs/task.json",sha256:"task-sha",size:10},
];
const html = (value:Run, artifacts=files) => renderToStaticMarkup(createElement(ReviewEvidence,{run:value,candidateId:"alpha",artifacts}));

describe("review evidence uses the exact frozen output",()=>{
  it("shows declared and executed expressions, original quote and actual zero/negative metrics",()=>{
    const value=html(run);
    for(const content of ["declared_close","executed_close","frozen original quote","frozen claim","模型推测","0.000000","-0.10%","50.00%","用于软件验证"])expect(value).toContain(content);
    expect(value).toContain("/api/runs/run/artifacts/current");
    expect(value).toContain("/api/runs/run/artifacts/report");
    expect(value).not.toContain("/api/runs/run/report");
    expect(value).toContain('data-review-revision="old-revision"');
    expect(value).toContain('data-review-attempt="attempt-2"');
    expect(value).toContain(`data-review-digest="${candidateDigest}"`);
  });
  it("filters prior attempts and other candidates while allowing verified redacted exports",()=>{
    expect(reviewEvidence(run,"alpha",files)?.outputFiles.map(file=>file.id)).toEqual(["current"]);
    expect(reviewEvidence(run,"alpha",[{...files[1],sha256:"redacted-export"}])?.outputFiles.map(file=>file.id)).toEqual(["current"]);
    expect(reviewEvidence(run,"alpha",[{...files[1],name:"candidates/alpha/attempt-9/factor.csv"}])?.outputFiles).toEqual([]);
    expect(reviewEvidence(run,"missing",files)).toBeNull();
  });
  it("suppresses retained metrics and downloads when verification or target disappears",()=>{
    for(const changed of [{...run,verification:{verified:false}},{...run,review_targets:[]},{...run,status:"running"}]){
      expect(reviewEvidence(changed,"alpha",files)).toBeNull();
      expect(html(changed)).toContain("当前输出尚未通过完整性校验或缺少审核目标");
      expect(html(changed)).not.toContain("候选计算指标");
      expect(html(changed)).not.toContain("下载因子 CSV");
    }
  });
  it("keeps missing frozen evidence unknown and uses engine failure reason without invented metrics",()=>{
    const changed={...run,state:{...run.state,task:undefined,candidates:[{...run.state!.candidates![0],status:"blocked",result:undefined,reason:"missing volume"}]}};
    expect(html(changed)).toContain("冻结任务未提供声明公式");
    expect(html(changed)).toContain("没有找到关联引文");
    expect(html(changed)).toContain("missing volume");
    expect(html(changed)).not.toContain("候选计算指标");
    expect(observedMetric(null)).toBe("未计算");
    expect(observedMetric(Infinity)).toBe("未计算");
  });
  it("keeps not-evaluable reasons visible even when a metrics object exists",()=>{
    const changed={...run,state:{...run.state,candidates:[{...run.state!.candidates![0],status:"not_evaluable",result:{metrics:{mean_rank_ic:null},reason:"constant signal across all assets"}}]}};
    expect(html(changed)).toContain("constant signal across all assets");
    expect(html(changed)).toContain("未计算");
    expect(html(changed)).toContain("研究假设（用户/项目修改）");
  });
});

describe("review downloads follow the frozen producer layout", () => {
  const main = () => {
    const value = structuredClone(run);
    const candidateId = "alpha101-101";
    Object.assign(value.state!.task!, {schema_version:3, execution:{kind:"real_daily_ohlc_development_v1"}});
    value.state!.task!.candidates![0].id = candidateId;
    value.state!.candidates![0].id = candidateId;
    value.state!.candidates![0].expression = value.state!.task!.candidates![0].expression;
    value.state!.candidates![0].result!.expression = value.state!.task!.candidates![0].expression;
    value.state!.candidates![0].artifacts = {"factor.csv":"raw-factor", "result.json":"raw-result"};
    value.review_targets![0].candidate_id = candidateId;
    delete value.state!.preflight;
    value.verification = {verified:true, state_digest:stateDigest};
    const artifacts: Artifact[] = [files[0], files[4], files[5],
      {id:"candidate",attempt_id:"attempt-2",name:"candidate.json",sha256:"candidate-export",size:10},
      {id:"factor",attempt_id:"attempt-2",name:"factor.csv",sha256:"factor-export",size:10},
      {id:"result",attempt_id:"attempt-2",name:"result.json",sha256:"redacted-result-export",size:10},
      {id:"prior-factor",attempt_id:"attempt-1",name:"factor.csv",sha256:"old-export",size:10},
    ];
    return {value, artifacts, candidateId};
  };
  const markup = (value:Run, candidateId:string, artifacts:Artifact[]) => renderToStaticMarkup(createElement(ReviewEvidence,{run:value,candidateId,artifacts}));
  const projection = () => ({schema_version:1,run_id:"run",revision_id:"old-revision",attempt_id:"attempt-2",state_digest:stateDigest,
    paper_id:"frozen-paper",document_sha256:pdfSha,evidence:[{id:"quote",page:15,quote:"frozen original quote",citation_verified:true}]});

  it("opens main physical pages only from a frozen-source projection bound to every selected identity", () => {
    const {value,artifacts,candidateId} = main();
    Object.assign(value,{frozen_source:projection()});
    const context=reviewEvidence(value,candidateId,artifacts)!;
    const source=context.evidenceReferences[0].source!;
    expect(source.paperId).toBe("frozen-paper"); expect(source.documentSha256).toBe(pdfSha);
    expect(source.href).toBe(`/api/runs/run/artifacts/pdf?inline=true&expected_attempt_id=attempt-2&expected_state_digest=${stateDigest}&expected_sha256=${pdfSha}#page=15`);
    expect(context.sourceIssues).toEqual([]);
    expect(value.state).not.toHaveProperty("preflight");
    const rendered=markup(value,candidateId,artifacts);
    expect(rendered).toContain("查看冻结论文第 15 物理页"); expect(rendered).toContain("frozen original quote");
    expect(rendered).not.toContain("/api/papers/");
  });

  it("rejects stale or malformed projection identities without falling back to another literal proof", () => {
    const mutations: Array<(proof:Record<string,unknown>)=>void> = [
      proof=>proof.schema_version=2, proof=>proof.run_id="another-run", proof=>proof.revision_id="latest-editor-revision",
      proof=>proof.attempt_id="attempt-1", proof=>proof.state_digest="c".repeat(64), proof=>proof.state_digest="invalid",
      proof=>proof.paper_id="", proof=>proof.document_sha256="invalid", proof=>proof.evidence=null,
    ];
    for (const mutate of mutations) {
      const {value,artifacts,candidateId}=main(), proof=projection(); mutate(proof);
      Object.assign(value,{frozen_source:proof}); value.state!.preflight=structuredClone(run.state!.preflight!);
      const context=reviewEvidence(value,candidateId,artifacts)!;
      expect(context.evidenceReferences[0].source).toBeUndefined();
      expect(context.sourceIssues.map(issue=>issue.code)).toContain("FROZEN_SOURCE_TARGET_MISMATCH");
      expect(context.frozenPaper).toBeUndefined();
      const rendered=markup(value,candidateId,artifacts);
      expect(rendered).toContain("候选计算指标"); expect(rendered).toContain("冻结 PDF 定位不可用"); expect(rendered).not.toContain("inline=true");
    }
    const {value,artifacts,candidateId}=main(); Object.assign(value,{frozen_source:{...projection(),document_sha256:"c".repeat(64)}});
    expect(reviewEvidence(value,candidateId,artifacts)!.evidenceReferences[0].source).toBeUndefined();
    expect(reviewEvidence(value,candidateId,artifacts)!.frozenPaper).toBeUndefined();
    const ordinary=structuredClone(run); Object.assign(ordinary,{frozen_source:projection()});
    expect(reviewEvidence(ordinary,"alpha",files)!.evidenceReferences[0].source).toBeUndefined();
  });

  it("does not invent page proof from a matching PDF when literal records are missing, changed or duplicated", () => {
    for (const mutate of [
      (proof:ReturnType<typeof projection>)=>proof.evidence=[],
      (proof:ReturnType<typeof projection>)=>proof.evidence[0].page=14,
      (proof:ReturnType<typeof projection>)=>proof.evidence[0].quote="another quotation",
      (proof:ReturnType<typeof projection>)=>proof.evidence[0].citation_verified=false,
      (proof:ReturnType<typeof projection>)=>proof.evidence.push({...proof.evidence[0]}),
    ]) {
      const {value,artifacts,candidateId}=main(), proof=projection(); mutate(proof); Object.assign(value,{frozen_source:proof});
      const context=reviewEvidence(value,candidateId,artifacts)!;
      expect(context.evidenceReferences[0].source).toBeUndefined();
      expect(context.frozenPaper?.id).toBe("pdf"); expect(context.outputFiles.map(file=>file.id)).toEqual(["factor","result"]);
      const rendered=markup(value,candidateId,artifacts);
      expect(rendered).toContain("冻结 PDF 定位不可用"); expect(rendered).not.toContain("inline=true");
      expect(rendered).not.toContain("冻结记录确认引文字面匹配");
    }
    const {value,artifacts,candidateId}=main(), proof=projection(); proof.evidence[0].quote="ｆｒｏｚｅｎ   original\n quote";
    Object.assign(value,{frozen_source:proof});
    expect(reviewEvidence(value,candidateId,artifacts)!.evidenceReferences[0].source).toBeDefined();
  });

  it("exposes the main PDF, task, candidate, root factor/result and report while missing literal proof stays unavailable", () => {
    const {value,artifacts,candidateId} = main();
    const context = reviewEvidence(value,candidateId,artifacts)!;
    expect(context.mainLayout).toBe(true);
    expect(context.outputFiles.map(file=>file.id)).toEqual(["factor","result"]);
    expect([context.frozenPaper?.id,context.taskFile?.id,context.candidateFile?.id,context.report?.id]).toEqual(["pdf","task","candidate","report"]);
    expect(context.candidateExportHref).toBeUndefined();
    expect(context.evidenceReferences[0].source).toBeUndefined();
    expect(context.sourceIssues.map(issue=>issue.code)).toContain("CITATION_PROOF_UNAVAILABLE");
    const rendered = markup(value,candidateId,artifacts);
    for (const id of ["pdf","task","candidate","factor","result","report"]) expect(rendered).toContain(`/api/runs/run/artifacts/${id}`);
    for (const label of ["下载本次冻结原 PDF","下载冻结任务 JSON","下载冻结候选 JSON","下载因子 CSV","下载计算结果 JSON","下载本次实验报告","冻结 PDF 定位不可用"]) expect(rendered).toContain(label);
    expect(rendered).not.toContain("prior-factor"); expect(rendered).not.toContain("inline=true"); expect(rendered).not.toContain("/api/papers/");
  });

  it("retains ordinary paths and exports only the selected frozen declaration from the task", () => {
    const context = reviewEvidence(run,"alpha",files)!;
    expect(context.mainLayout).toBe(false);
    expect(context.outputFiles.map(file=>file.id)).toEqual(["current"]);
    expect(context.candidateFile).toBeUndefined();
    const declaration = JSON.parse(decodeURIComponent(context.candidateExportHref!.split(",")[1]));
    expect(declaration).toEqual(run.state!.task!.candidates![0]);
    expect(declaration.expression).toBe("declared_close");
    expect(declaration).not.toHaveProperty("result");
    const rendered = html(run);
    expect(rendered).toContain("导出冻结候选声明"); expect(rendered).toContain("由本次冻结任务导出"); expect(rendered).toContain("/artifacts/task");
    const ambiguous = structuredClone(run); ambiguous.state!.task!.candidates!.push({...ambiguous.state!.task!.candidates![0]});
    expect(reviewEvidence(ambiguous,"alpha",files)!.candidateExportHref).toBeUndefined();
  });

  it("never borrows root computation files for an ordinary, unrecognized or multi-candidate producer", () => {
    for (const mutate of [
      (value:Run)=>Object.assign(value.state!.task!, {schema_version:1}),
      (value:Run)=>Object.assign(value.state!.task!, {execution:{kind:"unrecognized_producer"}}),
      (value:Run)=>value.state!.task!.candidates!.push({...value.state!.task!.candidates![0],id:"beta"}),
      (value:Run)=>value.state!.candidates!.push({...value.state!.candidates![0],id:"beta"}),
      (value:Run)=>value.state!.task!.candidates![0].hypothesis_id="different-hypothesis",
      (value:Run)=>value.state!.task!.candidates![0].expression="different-declared-expression",
      (value:Run)=>value.state!.candidates![0].result!.expression="different-executed-expression",
    ]) {
      const {value,artifacts,candidateId} = main(); mutate(value);
      expect(reviewEvidence(value,candidateId,artifacts)!.outputFiles).toEqual([]);
      expect(markup(value,candidateId,artifacts)).not.toContain("下载因子 CSV");
    }
    const ordinary = structuredClone(run); ordinary.state!.candidates![0].artifacts!["candidates/alpha/../factor.csv"]="sha";
    expect(reviewEvidence(ordinary,"alpha",[{...files[1],name:"candidates/alpha/../factor.csv"}])!.outputFiles).toEqual([]);
  });

  it("suppresses downloads from wrong attempts or inconsistent state targets while preserving verified metrics", () => {
    for (const mutate of [
      (value:Run)=>value.review_targets![0].attempt_id="attempt-9",
      (value:Run)=>value.review_targets![0].result_digest="invalid-candidate-digest",
      (value:Run)=>value.regression_target!.attempt_id="attempt-1",
      (value:Run)=>value.regression_target!.result_digest="invalid-state-digest",
      (value:Run)=>value.verification!.state_digest="c".repeat(64),
      (value:Run)=>delete value.regression_target,
    ]) {
      const {value,artifacts,candidateId} = main(); mutate(value);
      const context = reviewEvidence(value,candidateId,artifacts)!;
      expect(context.outputFiles).toEqual([]);
      expect([context.frozenPaper,context.taskFile,context.candidateFile,context.report]).toEqual([undefined,undefined,undefined,undefined]);
      const rendered=markup(value,candidateId,artifacts);
      expect(rendered).toContain("候选计算指标"); expect(rendered).not.toContain("下载因子 CSV");
      expect(rendered).not.toContain("下载本次冻结原 PDF");
    }
  });

  it("rejects duplicate names and duplicate artifact IDs without hiding unrelated downloads", () => {
    for (const name of ["inputs/paper.pdf","inputs/task.json","candidate.json","factor.csv","result.json","report.md"]) {
      for (const sameId of [false,true]) {
        const {value,artifacts,candidateId} = main();
        const original=artifacts.find(file=>file.name===name && file.attempt_id==="attempt-2")!;
        artifacts.push({...original,id:sameId?original.id:"duplicate",name:sameId?"unrelated.json":name});
        const context=reviewEvidence(value,candidateId,artifacts)!;
        const selected=[context.frozenPaper,context.taskFile,context.candidateFile,context.report,...context.outputFiles].filter(Boolean);
        expect(selected.some(file=>file?.id===original.id || file?.id==="duplicate")).toBe(false);
        expect(selected).toHaveLength(5);
      }
    }
    const duplicates=[...files,{...files[1],id:"duplicate-factor"},{...files[4],id:"duplicate-report"}];
    expect(reviewEvidence(run,"alpha",duplicates)!.outputFiles).toEqual([]);
    expect(reviewEvidence(run,"alpha",duplicates)!.report).toBeUndefined();
  });
});


describe("frozen source navigation fails closed independently of computed metrics", () => {
  const copy = () => structuredClone(run);
  it("binds the PDF to the exact frozen attempt/state/SHA and exposes source IDs", () => {
    const context = reviewEvidence(run, "alpha", files)!;
    const source = context.evidenceReferences[0].source!;
    const url = new URL(source.href, "http://localhost");
    expect(url.pathname).toBe("/api/runs/run/artifacts/pdf");
    expect(Object.fromEntries(url.searchParams)).toEqual({inline:"true",expected_attempt_id:"attempt-2",expected_state_digest:stateDigest,expected_sha256:pdfSha});
    expect(url.hash).toBe("#page=15");
    expect(source.paperId).toBe("frozen-paper");
    expect(context.sourceIssues).toEqual([]);
    const value=html(run);
    for (const part of ["候选来源链", "hypothesis", "quote", "PDF 物理页", pdfSha, "冻结声明", "不等于研究语义通过"]) expect(value).toContain(part);
    expect(value).not.toContain("/api/papers/");
  });
  it("rejects ambiguous computed candidate or review target identity", () => {
    const duplicateCandidate=copy(); duplicateCandidate.state!.candidates!.push({...duplicateCandidate.state!.candidates![0]});
    const duplicateTarget=copy(); duplicateTarget.review_targets!.push({...duplicateTarget.review_targets![0]});
    for (const value of [duplicateCandidate,duplicateTarget]) {
      expect(reviewEvidence(value,"alpha",files)).toBeNull();
      expect(html(value)).not.toContain("候选计算指标");
      expect(html(value)).not.toContain("inline=true");
    }
  });
  it("does not fabricate a hypothesis when declarations disagree with the executed association", () => {
    const value=copy(); value.state!.task!.candidates![0].hypothesis_id="another-hypothesis";
    const context=reviewEvidence(value,"alpha",files)!;
    expect(context.hypothesis).toBeUndefined(); expect(context.evidence).toEqual([]);
    expect(context.sourceIssues.map(issue=>issue.code)).toContain("HYPOTHESIS_MISMATCH");
    expect(html(value)).toContain("声明与执行的假说 ID 不一致");
    expect(html(value)).not.toContain("frozen claim"); expect(html(value)).not.toContain("inline=true");
    expect(html(value)).toContain("候选计算指标");
  });
  it("keeps partial missing references explicit while retaining independently bound quotations", () => {
    const value=copy(); value.state!.task!.hypotheses![0].evidence_ids.push("missing-quote");
    const context=reviewEvidence(value,"alpha",files)!;
    expect(context.evidenceReferences.map(reference=>reference.id)).toEqual(["quote","missing-quote"]);
    expect(context.evidenceReferences[0].source).toBeDefined();
    expect(context.evidenceReferences[1].issues).toEqual([{code:"EVIDENCE_MISSING",id:"missing-quote"}]);
    expect(html(value)).toContain("missing-quote"); expect(html(value)).toContain("未知");
    expect(html(value)).toContain("frozen original quote");
  });
  it("cannot choose the first duplicate declaration, hypothesis, evidence or reference", () => {
    const declaration=copy(); declaration.state!.task!.candidates!.push({...declaration.state!.task!.candidates![0]});
    const hypothesis=copy(); hypothesis.state!.task!.hypotheses!.push({...hypothesis.state!.task!.hypotheses![0]});
    const evidence=copy(); evidence.state!.task!.evidence!.push({...evidence.state!.task!.evidence![0]});
    const reference=copy(); reference.state!.task!.hypotheses![0].evidence_ids.push("quote");
    for (const value of [declaration,hypothesis,evidence,reference]) {
      const context=reviewEvidence(value,"alpha",files)!;
      expect(context.sourceIssues.length).toBeGreaterThan(0);
      expect(context.evidenceReferences.some(item=>item.source)).toBe(false);
      expect(html(value)).toContain("候选计算指标");
    }
  });
  it("accepts only normalized matching literal proof and never substitutes another proof", () => {
    const equivalent=copy(); (equivalent.state!.preflight!.evidence as Record<string,unknown>[])[0].quote="ｆｒｏｚｅｎ   original\n quote";
    expect(reviewEvidence(equivalent,"alpha",files)!.evidenceReferences[0].source).toBeDefined();
    for (const mutation of [(proof:Record<string,unknown>)=>proof.page=14,(proof:Record<string,unknown>)=>proof.quote="another quote",(proof:Record<string,unknown>)=>proof.citation_verified=false,(proof:Record<string,unknown>)=>proof.paper_id=""]) {
      const value=copy(); mutation((value.state!.preflight!.evidence as Record<string,unknown>[])[0]);
      expect(reviewEvidence(value,"alpha",files)!.evidenceReferences[0].source).toBeUndefined();
      expect(html(value)).toContain("冻结 PDF 定位不可用");
      expect(html(value)).toContain("候选计算指标");
    }
    const duplicate=copy(); (duplicate.state!.preflight!.evidence as unknown[]).push({...((duplicate.state!.preflight!.evidence as Record<string,unknown>[])[0])});
    expect(reviewEvidence(duplicate,"alpha",files)!.evidenceReferences[0].source).toBeUndefined();
  });
  it("requires a unique PDF from the selected attempt and a canonical state target", () => {
    const prior={...files[0],attempt_id:"attempt-1"};
    const wrong={...files[0],sha256:"c".repeat(64)};
    for(const artifacts of [[prior,...files.slice(1)],[wrong,...files.slice(1)],[...files,{...files[0],id:"pdf-duplicate"}],[...files,{...files[0],name:"another-file.pdf"}]]) {
      expect(reviewEvidence(run,"alpha",artifacts)!.evidenceReferences[0].source).toBeUndefined();
      expect(html(run,artifacts)).not.toContain("inline=true");
    }
    for(const target of [undefined,{attempt_id:"attempt-1",result_digest:stateDigest},{attempt_id:"attempt-2",result_digest:"not-a-digest"}]) {
      const value={...run,regression_target:target};
      expect(reviewEvidence(value,"alpha",files)!.evidenceReferences[0].source).toBeUndefined();
      expect(html(value)).toContain("候选计算指标");
    }
  });
  it("does not merge conflicting paper IDs just because their frozen PDF bytes match", () => {
    const value=copy();
    value.state!.task!.hypotheses![0].evidence_ids.push("quote-two");
    value.state!.task!.evidence!.push({id:"quote-two",page:15,quote:"second literal quote"});
    (value.state!.preflight!.evidence as Record<string,unknown>[]).push({id:"quote-two",page:15,quote:"second literal quote",paper_id:"different-frozen-paper",document_sha256:pdfSha,citation_verified:true});
    const context=reviewEvidence(value,"alpha",files)!;
    expect(context.evidenceReferences.every(reference=>!reference.source)).toBe(true);
    expect(context.sourceIssues.map(issue=>issue.code)).toContain("PAPER_IDENTITY_CONFLICT");
    const rendered=html(value);
    expect(rendered).toContain("关联引文的冻结论文 ID 不一致");
    expect(rendered).toContain("frozen original quote"); expect(rendered).toContain("second literal quote");
    expect(rendered).toContain("候选计算指标"); expect(rendered).not.toContain("inline=true");
  });
  it("keeps legacy quotation declarations readable without inventing a live PDF link", () => {
    const value=copy(); delete value.state!.preflight; delete value.regression_target;
    expect(html(value)).toContain("frozen original quote");
    expect(html(value)).toContain("冻结 PDF 定位不可用");
    expect(html(value)).not.toContain("/api/papers/"); expect(html(value)).not.toContain("inline=true");
    expect(html(value)).toContain("候选计算指标");
  });
  it("preserves unknown provenance values as declarations and a long quotation remains accessible", () => {
    const value=copy(); (value.state!.task!.candidates![0] as unknown as Record<string,unknown>).origin="legacy_unclassified";
    value.state!.task!.evidence![0].quote="Very long quoted text. ".repeat(60);
    const rendered=html(value);
    expect(rendered).toContain("legacy_unclassified（未识别声明）");
    expect(rendered).toContain("展开完整引文"); expect(rendered).toContain(value.state!.task!.evidence![0].quote);
    expect(rendered).not.toContain("inline=true");
  });
  it("shows a compact candidate-to-hypothesis-to-evidence overview without changing selection", () => {
    const value=renderToStaticMarkup(createElement(CandidateComparison,{run,candidates:run.state!.candidates!,artifacts:files,selected:"alpha",disabled:false,onSelect:()=>{throw new Error("render must not select");}}));
    for(const content of ["声明来源链","用户/项目修改","hypothesis","quote","-0.10%"] )expect(value).toContain(content);
  });
});
