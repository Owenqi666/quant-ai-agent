import {describe,expect,it} from "vitest";
import type {CaseDetail,CasePreview,CaseResult} from "./generated/api-contract";
import {caseCreateBody,caseProposalBody,resultHighlights} from "./researchCases";

describe("research context stays bound to saved results",()=>{
  const preview={source_kind:"author_study",source_id:"study",source_digest:"a".repeat(64)} as CasePreview;
  const detail={context:{allowed_actions:["stop_data_insufficient"],evidence:[{id:"mom-window"}]}} as CaseDetail;
  it("uses the verified source digest without client-produced counts",()=>{
    expect(caseCreateBody(preview," Case ","note")).toEqual({...preview,title:"Case",note:"note"});
    expect(()=>caseCreateBody(null,"Case","")).toThrow();
  });
  it("rejects a different action or invented evidence instead of changing the gate",()=>{
    expect(()=>caseProposalBody(detail,"request_human_review","Proceed",[])).toThrow();
    expect(()=>caseProposalBody(detail,"stop_data_insufficient","Stop",["invented"])).toThrow();
    expect(caseProposalBody(detail,"stop_data_insufficient","Keep the declared gate",["mom-window"])).toEqual({tool:"propose_next_action",arguments:{action:"stop_data_insufficient",rationale:"Keep the declared gate",evidence_ids:["mom-window"]}});
  });
  it("displays recorded zero and null separately without calculating metrics",()=>{
    const result={payload_json:JSON.stringify({summary:{months:12,months_meeting_threshold:0,score:null}})} as CaseResult;
    expect(resultHighlights(result)).toEqual([{key:"months",value:"12"},{key:"months_meeting_threshold",value:"0"},{key:"score",value:"未计算 / 不可用"}]);
    expect(resultHighlights({payload_json:"invalid"} as CaseResult)).toEqual([]);
  });
});
