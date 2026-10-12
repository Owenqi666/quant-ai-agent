import {describe,expect,it} from "vitest";
import {buildProposalCreate,buildProposalHandoff,observedMetric,proposalCanCancel,proposalCanHandoff,proposalIsActive,reviewMatchesProposal} from "./providerWorkflow";
import type {ProposalContextResponse,ProposalDetail,ProvidersResponse} from "./generated/api-contract";

const context={context:{research_id:"research",base_revision_id:"base",source_digest:"frozen-source"}} as ProposalContextResponse;
const providers={schema_version:1,allow_fixture:true,allow_real:false,real_dispatch_enabled:false,dispatch_paused:false,providers:[{
  config_id:"fixture",mode:"fixture",enabled:true,dispatchable:true,model:"fixture-model",reasoning_effort:"low",
  expected_cli_version:"0.145.0",compatibility_verified:false,tool_boundary_verified:false,inference_verified:false,readiness:null,error:null,
}]} satisfies ProvidersResponse;
const ready={id:"proposal",research_id:"research",base_revision_id:"base",status:"ready",accepted_response_digest:"response-digest",
  research_job_id:null,accepted_response:{outcome:"draft"}} as ProposalDetail;

describe("Provider workflow uses the real frozen task and explicit transitions",()=>{
  it("binds one generation to its selected context/config with no host settings in the browser body",()=>{
    const body=buildProposalCreate("research","base",context,providers,"fixture","implement maintained evidence");
    expect(body).toMatchObject({research_id:"research",base_revision_id:"base",expected_source_digest:"frozen-source",candidate_limit:1,provider_config_id:"fixture"});
    expect(body.budget.max_provider_invocations).toBe(1);
    for(const key of ["executable","model","allow_real","compatibility_verified","tool_boundary_verified"])expect(body).not.toHaveProperty(key);
  });
  it("rejects a stale research/revision, paused queue and unavailable provider",()=>{
    expect(()=>buildProposalCreate("other","base",context,providers,"fixture","")).toThrow("不匹配");
    expect(()=>buildProposalCreate("research","next",context,providers,"fixture","")).toThrow("不匹配");
    expect(()=>buildProposalCreate("research","base",context,{...providers,dispatch_paused:true},"fixture","")).toThrow("不能接收");
    expect(()=>buildProposalCreate("research","base",context,providers,"missing","")).toThrow("不能接收");
  });
  it("does not turn configured/verified booleans into host permission",()=>{
    const declared={...providers,allow_fixture:false,providers:[{...providers.providers[0],mode:"codex_cli" as const,compatibility_verified:true,inference_verified:true,tool_boundary_verified:true}]};
    expect(()=>buildProposalCreate("research","base",context,declared,"fixture","")).toThrow("不能接收");
    expect(()=>buildProposalCreate("research","base",context,{...declared,allow_real:true,real_dispatch_enabled:true},"fixture","")).not.toThrow();
    expect(()=>buildProposalCreate("research","base",context,{...providers,allow_fixture:false},"fixture","")).toThrow("不能接收");
  });
  it("only permits the ready actual draft to be handed off and never asserts research approval",()=>{
    expect(proposalCanHandoff(ready)).toBe(true);
    const body=buildProposalHandoff(ready);
    expect(body.accepted_response_digest).toBe("response-digest");
    expect(body.note).toContain("仍待人工评审");
    expect(body.allow_partial_execution).toBe(false);
    for(const status of ["running","abstained","rejected","failed","handed_off"]){
      const value={...ready,status} as ProposalDetail;
      expect(proposalCanHandoff(value)).toBe(false);expect(()=>buildProposalHandoff(value)).toThrow();
    }
    expect(proposalCanHandoff({...ready,accepted_response:null})).toBe(false);
  });
  it("polls active tasks and only offers cancellation before terminal output",()=>{
    for(const status of ["queued","running","validating","cancelling","handing_off"])expect(proposalIsActive(status)).toBe(true);
    for(const status of ["ready","handed_off","abstained","failed","interrupted"])expect(proposalIsActive(status)).toBe(false);
    expect(proposalCanCancel("running")).toBe(true);expect(proposalCanCancel("cancelling")).toBe(false);expect(proposalCanCancel("handed_off")).toBe(false);
  });
  it("discards review material from another request/source/status or response",()=>{
    const review={proposal:ready};
    expect(reviewMatchesProposal(review,ready)).toBe(true);
    const changes:Array<Partial<ProposalDetail>>=[{id:"other"},{research_id:"other"},{base_revision_id:"next"},{status:"rejected"},{accepted_response_digest:"other"},{research_job_id:"new-job"}];
    for(const change of changes){
      expect(reviewMatchesProposal({proposal:{...ready,...change}},ready)).toBe(false);
    }
  });
  it("preserves real zero/negative numbers and distinguishes missing metrics",()=>{
    expect(observedMetric(0)).toBe("0");expect(observedMetric(-0.123456789)).toBe("-0.123456789");
    expect(observedMetric(null)).toBe("缺失");expect(observedMetric(undefined)).toBe("缺失");
  });
});
