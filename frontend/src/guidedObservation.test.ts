import { describe, expect, it } from "vitest";
import { beginGuidedSession, buildGuidedObservation, changeGuidedTimer, completeGuidedSubmission, finishGuidedSession, guidedKey, initialGuidedDraft, interruptGuidedSession, loadGuidedDraft, saveGuidedDraft } from "./guidedObservation";
import type { GuidedDraft } from "./guidedObservation";

const protocol={id:"fixed-task",digest:"a".repeat(64),taskSha256:"b".repeat(64)};
const t=(second:number)=>`2026-01-01T00:00:${String(second).padStart(2,"0")}Z`;
function configured():GuidedDraft {
  const d=initialGuidedDraft();d.settings={...d.settings,participant:"tester",familiarity:"fixture only",codeCommit:"c".repeat(40),
    environment:{machine:"fixture",os:"fixture",python:"fixture",dependencies:"fixture"}};
  return d;
}
function started(){return beginGuidedSession(configured(),protocol,"revision",t(0),"00000000-0000-4000-8000-000000000001");}
function storage(){const values=new Map<string,string>();return {getItem:(k:string)=>values.get(k)??null,setItem:(k:string,v:string)=>{values.set(k,v);}};}

describe("guided observation session boundaries",()=>{
  it("starts as practice and requires declared metadata without fabricating commit or familiarity",()=>{
    const initial=initialGuidedDraft();expect(initial.settings.practice).toBe(true);expect(initial.settings.codeCommit).toBe("");
    expect(()=>beginGuidedSession(initial,protocol,"revision",t(0),"00000000-0000-4000-8000-000000000001")).toThrow(/填写/);
    const missing=configured();missing.settings.codeCommit="";expect(()=>beginGuidedSession(missing,protocol,"revision",t(0),"00000000-0000-4000-8000-000000000001")).toThrow(/40 位/);
    const d=configured(),run=beginGuidedSession(d,protocol,"revision",t(0),"00000000-0000-4000-8000-000000000001");
    d.settings.environment.os="changed";expect(run.session?.settings.environment.os).toBe("fixture");
    expect(run.session?.open).toEqual({phase:"reading",kind:"active",startedAt:t(0)});
    expect(()=>beginGuidedSession(run,protocol,"revision",t(1),"00000000-0000-4000-8000-000000000002")).toThrow(/先完成/);
  });
  it("phase and kind changes close exactly one interval with no overlap; pause adds no invented time",()=>{
    let session=started().session!;
    session=changeGuidedTimer(session,t(3),"hypothesis","active");
    session=changeGuidedTimer(session,t(6),"hypothesis","waiting");
    session=changeGuidedTimer(session,t(8),"hypothesis",null);
    session=changeGuidedTimer(session,t(12),"review",null);
    session=changeGuidedTimer(session,t(15),"review","active");
    session=finishGuidedSession(session,t(20));
    expect(session.intervals.map(i=>[i.kind,i.started_at,i.ended_at])).toEqual([
      ["active",t(0),t(3)],["active",t(3),t(6)],["waiting",t(6),t(8)],["active",t(15),t(20)],
    ]);
    expect(session.intervals.every(i=>i.source_reference===null)).toBe(true);
    expect(()=>changeGuidedTimer(session,t(21),"report","active")).toThrow(/已结束/);
  });
  it("rejects clock reversal and interval exhaustion without altering the original",()=>{
    const original=started().session!;
    expect(()=>changeGuidedTimer(original,t(0),"reading",null)).toThrow(/时间/);expect(original.intervals).toEqual([]);
    const full={...original,open:null,intervals:Array.from({length:300},()=>({phase:"reading" as const,kind:"active" as const,started_at:t(0),ended_at:t(1),status:"ended" as const,source_reference:null}))};
    expect(()=>changeGuidedTimer(full,t(2),"reading","active")).toThrow(/300/);
  });
  it("reload converts open interval to unknown end and prohibits resuming that session",()=>{
    const d=started();d.session=changeGuidedTimer(d.session!,t(5),"hypothesis","active");
    const s=storage(),key=guidedKey("workspace","research");saveGuidedDraft(s,key,d);
    const recovered=loadGuidedDraft(s,key)!;
    expect(recovered.session?.open).toBeNull();expect(recovered.session?.interrupted).toBe(true);
    expect(recovered.session?.intervals).toHaveLength(2);
    expect(recovered.session?.intervals[1]).toMatchObject({started_at:t(5),ended_at:null,status:"interrupted"});
    expect(()=>changeGuidedTimer(recovered.session!,t(10),"reading","active")).toThrow(/中断/);
    expect(interruptGuidedSession(recovered)).toEqual(recovered);
  });
  it("paused drafts recover without adding away time and keep research scopes separate",()=>{
    const d=started();d.session=changeGuidedTimer(d.session!,t(5),"reading",null);
    const s=storage(),key=guidedKey("w","r");saveGuidedDraft(s,key,d);
    expect(loadGuidedDraft(s,key)).toEqual(d);expect(loadGuidedDraft(s,guidedKey("w","other"))).toBeNull();
    s.setItem("bad",s.getItem(key)!);expect(()=>loadGuidedDraft(s,"bad")).toThrow(/作用域/);
    expect(()=>saveGuidedDraft({getItem:()=>null,setItem:()=>{}},key,d)).toThrow(/保存/);
  });
  it("only builds an observation after explicit finish and preserves missing measurements",()=>{
    const d=started(),outputs={task_sha256:protocol.taskSha256,revision_id:"revision",run_id:null,attempt_id:null,review_ids:[],external_run_reference:null,verification_evidence:null,report_reference:null};
    expect(()=>buildGuidedObservation(d,{},outputs,null)).toThrow(/先结束/);
    d.session=finishGuidedSession(d.session!,t(5));expect(()=>buildGuidedObservation(d,{},outputs,null)).toThrow(/原因/);
    d.reason="Fixture stopped before remaining phases";
    const result=buildGuidedObservation(d,{},outputs,null);
    expect(result.source).toBe("human");expect(result.completion).toBe("incomplete");expect(result.practice_session).toBe(true);
    expect(result.intervals).toHaveLength(1);expect(result.bound_outputs.run_id).toBeNull();expect(result.dimensions).toBeNull();
    // Unit-only construction does not create a server observation or a human evidence claim.
  });
  it("submission cleanup requires the exact submitted snapshot, never only the same session ID",()=>{
    const s=storage(),key=guidedKey("w","r"),d=started();d.session=finishGuidedSession(d.session!,t(5));d.reason="original";
    const submitted=JSON.stringify(d);saveGuidedDraft(s,key,d);
    const newer={...d,reason:"new unsent edits"};saveGuidedDraft(s,key,newer);
    expect(completeGuidedSubmission(s,key,d.session!.id,submitted)).toBeNull();expect(loadGuidedDraft(s,key)?.reason).toBe("new unsent edits");
    expect(completeGuidedSubmission(s,key,d.session!.id,JSON.stringify(newer))).not.toBeNull();
    const next=loadGuidedDraft(s,key)!;expect(next.session).toBeNull();expect(next.settings.condition).toBe("manual_cli");expect(next.settings.executionOrder).toBe(2);
    expect(next.settings.environment).toEqual(d.settings.environment);
  });
  it("normal partial metadata remains editable; un-restorable or malicious drafts are never written",()=>{
    const s=storage(),key=guidedKey("w","r"),d=initialGuidedDraft();d.settings.codeCommit="INCOMPLETE";saveGuidedDraft(s,key,d);
    expect(loadGuidedDraft(s,key)?.settings.codeCommit).toBe("INCOMPLETE");
    const before=s.getItem(key);d.settings.familiarity="x".repeat(2001);expect(()=>saveGuidedDraft(s,key,d)).toThrow();expect(s.getItem(key)).toBe(before);
    const attack=started();attack.session!.id="$(touch /tmp/unwanted)";expect(()=>saveGuidedDraft(s,key,attack)).toThrow();
    const corrupt=started();corrupt.session!.open!.startedAt="bad timestamp";expect(()=>saveGuidedDraft(s,key,corrupt)).toThrow();
    const unknown=started();(unknown.session!.protocol as unknown as Record<string,unknown>).extra="unsafe";expect(()=>saveGuidedDraft(s,key,unknown)).toThrow();
  });
});
