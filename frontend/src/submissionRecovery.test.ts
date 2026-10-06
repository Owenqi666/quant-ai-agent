import { describe, it, expect } from "vitest";
import { creationDraftKey } from "./submissionRecovery";
import { draftKey } from "./drafts";
import { stageMutation, readMutation } from "./reviewRecovery";

describe("creation request recovery", () => {
  it("retains the original copy draft without sending local context in the body", () => {
    const saved = new Map<string, string>();
    const storage = { getItem: (k: string) => saved.get(k) ?? null, setItem: (k: string, v: string) => { saved.set(k, v); } };
    const context = { draft_key: draftKey("workspace", "create:research:revision") };
    const request = stageMutation(storage, "pending", "/researches", {title:"source"}, "key", context);
    context.draft_key = "changed";
    expect(request.body).toEqual({title:"source",idempotency_key:"key"});
    expect(creationDraftKey("workspace", readMutation(storage,"pending","/researches")!.context))
      .toBe(draftKey("workspace", "create:research:revision"));
  });
  it("rejects absent, cross-workspace and non-creation draft identities", () => {
    for (const value of [undefined, {}, {draft_key:42}, {draft_key:draftKey("elsewhere","create:blank")}, {draft_key:draftKey("workspace","revision:one")}])
      expect(() => creationDraftKey("workspace",value)).toThrow();
  });
});

import { acknowledgeMutation, rejectMutation } from "./reviewRecovery";
describe("late response ownership", () => {
  function memory() {
    const items = new Map<string,string>();
    return { getItem:(key:string)=>items.get(key)??null, setItem:(key:string,value:string)=>{items.set(key,value);}, removeItem:(key:string)=>{items.delete(key);} };
  }
  it("does not let an old rejection overwrite a newer pending operation", () => {
    const s=memory(); const a=stageMutation(s,"pending","/researches",{title:"a"},"a");
    acknowledgeMutation(s,"pending","/researches",a);
    const b=stageMutation(s,"pending","/researches",{title:"b"},"b");
    expect(rejectMutation(s,"pending","/researches",a)).toBeNull();
    expect(readMutation(s,"pending","/researches")).toEqual(b);
  });
  it("checks ownership before deleting any draft and skips already acknowledged cleanup", () => {
    const s=memory(); const a=stageMutation(s,"pending","/researches",{title:"a"},"a");
    acknowledgeMutation(s,"pending","/researches",a);
    s.setItem("draft","new unsent draft");
    acknowledgeMutation(s,"pending","/researches",a,()=>s.removeItem("draft"));
    expect(s.getItem("draft")).toBe("new unsent draft");
    stageMutation(s,"pending","/researches",{title:"b"},"b");
    expect(()=>acknowledgeMutation(s,"pending","/researches",a,()=>s.removeItem("draft"))).toThrow();
    expect(s.getItem("draft")).toBe("new unsent draft");
  });
  it("retains the original pending request when owned draft cleanup fails", () => {
    const s=memory(); const a=stageMutation(s,"pending","/researches",{title:"a"},"a");
    expect(()=>acknowledgeMutation(s,"pending","/researches",a,()=>{throw new Error("storage unavailable");})).toThrow();
    expect(readMutation(s,"pending","/researches")).toEqual(a);
  });
});
