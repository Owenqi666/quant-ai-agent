import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Artifact, Run } from "./domain";
import { observedMetric, reviewEvidence } from "./reviewEvidence";
import ReviewEvidence from "./components/ReviewEvidence";

const run: Run = {
  id:"run", research_id:"research", revision_id:"old-revision", status:"completed", mode:"normalized_fixed", created_at:"", attempt_count:2,
  verification:{verified:true}, review_targets:[{candidate_id:"alpha",attempt_id:"attempt-2",result_digest:"digest"}],
  state:{task:{candidates:[{id:"alpha",hypothesis_id:"hypothesis",expression:"declared_close",origin:"user_modification",changes:[]}],
    hypotheses:[{id:"hypothesis",claim:"frozen claim",attribution:"user_modification",economic_mechanism:"conjecture",mechanism_attribution:"model_conjecture",signal_direction:"higher long",required_fields:["close"],assumptions:[],evidence_ids:["quote"]}],
    evidence:[{id:"quote",page:15,quote:"frozen original quote"}]},
    candidates:[{id:"alpha",hypothesis_id:"hypothesis",expression:"close",origin:"user_modification",changes:[],status:"evaluated",artifacts:{"candidates/alpha/attempt-1/factor.csv":"sha"},
      result:{expression:"executed_close",metrics:{mean_rank_ic:0,mean_gross_return:-0.001,factor_coverage:0.5,evaluated_days:4},market_metadata:{data_kind:"synthetic",version:"fixture"}}}]},
};
const files: Artifact[] = [
  {id:"current",attempt_id:"attempt-2",name:"candidates/alpha/attempt-1/factor.csv",sha256:"sha",size:10},
  {id:"old",attempt_id:"attempt-1",name:"candidates/alpha/attempt-1/factor.csv",sha256:"sha",size:10},
  {id:"other",attempt_id:"attempt-2",name:"candidates/beta/attempt-1/factor.csv",sha256:"sha",size:10},
  {id:"report",attempt_id:"attempt-2",name:"report.md",sha256:"report-sha",size:10},
];
const html = (value:Run) => renderToStaticMarkup(createElement(ReviewEvidence,{run:value,candidateId:"alpha",paperId:"paper",artifacts:files}));

describe("review evidence uses the exact frozen output",()=>{
  it("shows declared and executed expressions, original quote and actual zero/negative metrics",()=>{
    const value=html(run);
    for(const content of ["declared_close","executed_close","frozen original quote","frozen claim","模型推测","0.000000","-0.10%","50.00%","用于软件验证"])expect(value).toContain(content);
    expect(value).toContain("/api/runs/run/artifacts/current");
    expect(value).toContain("/api/runs/run/artifacts/report");
    expect(value).not.toContain("/api/runs/run/report");
  });
  it("filters prior attempts and other candidates while allowing verified redacted exports",()=>{
    expect(reviewEvidence(run,"alpha",files)?.outputFiles.map(file=>file.id)).toEqual(["current"]);
    expect(reviewEvidence(run,"alpha",[{...files[0],sha256:"redacted-export"}])?.outputFiles.map(file=>file.id)).toEqual(["current"]);
    expect(reviewEvidence(run,"alpha",[{...files[0],name:"candidates/alpha/attempt-9/factor.csv"}])?.outputFiles).toEqual([]);
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
    expect(html(changed)).toContain("研究假设（本人修改）");
  });
});
