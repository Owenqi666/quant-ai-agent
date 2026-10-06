import {describe,it,expect} from "vitest";
import {emptySemanticDraft,readSemanticDraft,saveSemanticDraft} from "./semanticAnnotationDraft";

function storage(){const values=new Map<string,string>();return {getItem:(key:string)=>values.get(key)??null,setItem:(key:string,value:string)=>{values.set(key,value);}};}
describe("material-bound drafts",()=>{
  it("retains explicit unknown decisions and does not invent a source or reviewer",()=>{const s=storage(),d=emptySemanticDraft();saveSemanticDraft(s,"key","material-a",d);expect(readSemanticDraft(s,"key","material-a")).toEqual(d);expect(Object.values(d.dimensions).every(v=>v.outcome===""&&v.reason==="")).toBe(true);expect(d.source).toBe("");});
  it("refuses a draft copied to a different material version",()=>{const s=storage();saveSemanticDraft(s,"key","a",emptySemanticDraft());expect(()=>readSemanticDraft(s,"key","b")).toThrow();});
  it("refuses corrupt or incomplete five-dimensional drafts",()=>{const s=storage(),d=emptySemanticDraft();s.setItem("k",JSON.stringify({version:1,material:"a",draft:{...d,dimensions:{evidence_accuracy:{outcome:"passed",reason:"invalid incomplete draft"}}}}));expect(()=>readSemanticDraft(s,"k","a")).toThrow();s.setItem("k","{");expect(()=>readSemanticDraft(s,"k","a")).toThrow();});
  it("refuses lost durable writes before the draft can be offered for submission",()=>{const s={getItem:()=>null,setItem:()=>{}};expect(()=>saveSemanticDraft(s,"k","a",emptySemanticDraft())).toThrow();});
});
