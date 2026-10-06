import {assessmentDimensions} from "./researchAssessment";
import type {Dimension} from "./researchAssessment";

export type SemanticDraft={source:""|"human"|"automation";reviewer:string;confirmed_at:string;supersedes_id:string;dimensions:Record<Dimension,{outcome:string;reason:string}>};
export function emptySemanticDraft():SemanticDraft{return {source:"",reviewer:"",confirmed_at:"",supersedes_id:"",dimensions:Object.fromEntries(Object.keys(assessmentDimensions).map(k=>[k,{outcome:"",reason:""}])) as SemanticDraft["dimensions"]};}
export function readSemanticDraft(storage:Pick<Storage,"getItem">,key:string,material:string):SemanticDraft|null{
  const raw=storage.getItem(key);if(raw===null)return null;if(raw.length>32000)throw new Error("标注草稿超出长度限制，请先保留原记录。");
  const v=JSON.parse(raw),d=v.draft;
  if(v.version!==1||v.material!==material||!d||!["","human","automation"].includes(d.source)||["reviewer","confirmed_at","supersedes_id"].some(k=>typeof d[k]!=="string")||!d.dimensions)throw new Error("标注草稿版本或材料身份不一致。");
  if(d.reviewer.length>120||d.confirmed_at.length>40||d.supersedes_id.length>100)throw new Error("标注草稿字段超出限制。");
  if(Object.keys(d.dimensions).sort().join()!==Object.keys(assessmentDimensions).sort().join()||Object.values(d.dimensions).some((x)=>{const r=x as {outcome?:unknown;reason?:unknown};return !["","passed","failed","not_assessed","not_applicable"].includes(String(r.outcome))||typeof r.reason!=="string"||r.reason.length>2000;}))throw new Error("标注草稿维度不完整或损坏。");
  return d as SemanticDraft;
}
export function saveSemanticDraft(storage:Pick<Storage,"getItem"|"setItem">,key:string,material:string,draft:SemanticDraft){const text=JSON.stringify({version:1,material,draft});storage.setItem(key,text);if(storage.getItem(key)!==text)throw new Error("无法持久保存标注草稿，提交尚未发送。");}
