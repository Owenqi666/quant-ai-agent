import { describe,expect,it } from "vitest";
import { clearSubmittedDraft,issueDecisionPath } from "./taskRecovery";
import { readMutation,recoveryKey,stageMutation } from "./reviewRecovery";

function storage(){const values=new Map<string,string>();return {getItem:(key:string)=>values.get(key)??null,setItem:(key:string,value:string)=>values.set(key,value),removeItem:(key:string)=>{values.delete(key);}};}
describe("revision and issue request ownership",()=>{
  it("clears submitted draft bytes while preserving a newer draft under the same key",()=>{
    const saved=storage();saved.setItem("draft","original");
    clearSubmittedDraft(saved,"draft",{draftKey:"draft",draftRaw:"original"});expect(saved.getItem("draft")).toBeNull();
    saved.setItem("draft","new user edits");clearSubmittedDraft(saved,"draft",{draftKey:"draft",draftRaw:"original"});
    expect(saved.getItem("draft")).toBe("new user edits");
    clearSubmittedDraft(saved,"draft",{draftKey:"draft",draftRaw:null});expect(saved.getItem("draft")).toBe("new user edits");
    expect(()=>clearSubmittedDraft(saved,"another-workspace",{draftKey:"draft",draftRaw:"new user edits"})).toThrow("作用域不匹配");
    expect(saved.getItem("draft")).toBe("new user edits");
  });
  it("recovers an issue decision route independently of the currently selected issue",()=>{
    const saved=storage(),key=recoveryKey("workspace-a","issue-event-request","research");
    stageMutation(saved,key,"/issues/original/events",{base_event_id:"original-event",state:"proposed"},"stable-key",{issueId:"original"});
    expect(issueDecisionPath(saved,key,"new-selection")).toBe("/issues/original/events");
    expect(readMutation(saved,key,issueDecisionPath(saved,key,""))?.body.idempotency_key).toBe("stable-key");
    expect(issueDecisionPath(saved,recoveryKey("workspace-b","issue-event-request","research"),"new-selection")).toBe("/issues/new-selection/events");
  });
  it("never adopts an unbound or foreign route, and leaves malformed records available for diagnosis",()=>{
    const saved=storage(),key="scope";
    const raw=JSON.stringify({version:1,scope:key,path:"/issues/other/events",savedAt:"date",body:{idempotency_key:"key"},state:"unconfirmed",context:{issueId:"original"}});
    saved.setItem(key,raw);expect(issueDecisionPath(saved,key,"current")).toBe("/issues/current/events");
    expect(()=>readMutation(saved,key,"/issues/current/events")).toThrow();expect(saved.getItem(key)).toBe(raw);
    saved.setItem(key,"broken JSON");expect(issueDecisionPath(saved,key,"current")).toBe("/issues/current/events");
    expect(saved.getItem(key)).toBe("broken JSON");
  });
});
