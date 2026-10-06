import type { ProtocolConfig } from "./generated/api-contract";
import { removeDurably, writeDurably } from "./reviewRecovery";

export const projectProtocol = ():ProtocolConfig => ({schema_version:1,mode:"project",mom_window_months:11,mom_skip_months:1,
  id_window_months:11,id_skip_months:1,missing_policy:"complete",min_coverage:1,max_missing_run:0,zero_policy:"include",fill_policy:"none"});
export const protocolFields = {mode:"规则身份",mom_window_months:"MOM 回看月数",mom_skip_months:"MOM 跳过月数",id_window_months:"ID 回看月数",id_skip_months:"ID 跳过月数",missing_policy:"缺失规则",min_coverage:"最低有效覆盖率",max_missing_run:"最大连续缺失交易日",zero_policy:"零收益规则",fill_policy:"填补规则",schema_version:"格式版本"};
const integer=(value:unknown,min:number,max:number)=>typeof value==="number"&&Number.isInteger(value)&&value>=min&&value<=max;
/** Client checks only prevent ambiguous inputs; the server validates the complete scientific contract. */
export function protocolConfigError(config:ProtocolConfig):string {
  if(config.schema_version!==1||config.zero_policy!=="include"||config.fill_policy!=="none")return "规则版本、零收益或填补约定不受支持。";
  if(!integer(config.mom_window_months,1,36)||!integer(config.mom_skip_months,0,12))return "MOM 回看月数须为 1–36，跳过月数须为 0–12 的整数。";
  if(config.mode==="paper")return config.mom_window_months===11&&config.mom_skip_months===1&&config.id_window_months===null&&config.id_skip_months===null&&config.missing_policy==="unresolved"&&config.min_coverage===null&&config.max_missing_run===null?"":"论文原文预设不可改写；请先明确切换为项目变体。";
  if(config.mode!=="project"||!integer(config.id_window_months,1,36)||!integer(config.id_skip_months,0,12))return "ID 回看月数须为 1–36，跳过月数须为 0–12 的整数。";
  if(config.missing_policy==="complete")return config.min_coverage===1&&config.max_missing_run===0?"":"完整观察规则要求覆盖率 1，连续缺失日为 0。";
  if(config.missing_policy!=="available"||typeof config.min_coverage!=="number"||!Number.isFinite(config.min_coverage)||config.min_coverage<=0||config.min_coverage>1||!integer(config.max_missing_run,0,366))return "可用观察规则要求覆盖率大于 0 且不超过 1，连续缺失日为 0–366 的整数。";
  return "";
}
export function protocolMonthError(month:string):string {
  return /^(19(?:0[5-9]|[1-9]\d)|20\d{2})-(0[1-9]|1[0-2])$/.test(month)?"":"目标月份须为 1905–2099 年间的 YYYY-MM。";
}
export function protocolViewKey(config:ProtocolConfig,month:string):string {
  return JSON.stringify([month,...Object.keys(protocolFields).map(key=>config[key as keyof ProtocolConfig])]);
}
export type ProtocolSaveBody={title:string;note:string;parent_id:string|null;config:ProtocolConfig};
export type PendingProtocol={version:1;scope:string;body:ProtocolSaveBody;state:"unconfirmed"|"rejected"};
export const protocolRecoveryKey=(workspace:string)=>`paper-alpha:protocol-save:v1:${JSON.stringify(workspace)}`;
type ReadStorage=Pick<Storage,"getItem">;
function isRecord(value:unknown):value is Record<string,unknown>{return !!value&&typeof value==="object"&&!Array.isArray(value);}
export function readProtocolSave(storage:ReadStorage,key:string):PendingProtocol|null {
  const raw=storage.getItem(key);if(!raw)return null;
  const value:unknown=JSON.parse(raw);
  if(!isRecord(value)||value.version!==1||value.scope!==key||!isRecord(value.body)||!isRecord(value.body.config)||
    !["unconfirmed","rejected"].includes(String(value.state))||typeof value.body.title!=="string"||!value.body.title.trim()||typeof value.body.note!=="string"||
    !(value.body.parent_id===null||typeof value.body.parent_id==="string"&&/^protocol_[0-9a-f]{64}$/.test(value.body.parent_id))||
    Object.keys(value.body).some(field=>!["title","note","parent_id","config"].includes(field))||
    Object.keys(value.body.config).some(field=>!Object.hasOwn(protocolFields,field))||
    protocolConfigError(value.body.config as unknown as ProtocolConfig))throw new Error("原保存请求损坏或作用域不匹配；已阻止新保存，请保留本机记录检查。");
  return value as unknown as PendingProtocol;
}
export function stageProtocolSave(storage:ReadStorage&Pick<Storage,"setItem">,key:string,body:ProtocolSaveBody):PendingProtocol {
  if(storage.getItem(key))throw new Error("已有待确认的规则版本，请先确认原保存请求。");
  const error=protocolConfigError(body.config);if(error)throw new Error(error);
  const pending:PendingProtocol={version:1,scope:key,body:structuredClone(body),state:"unconfirmed"};
  writeDurably(storage,key,pending);return pending;
}
export function finishProtocolSave(storage:ReadStorage&Pick<Storage,"removeItem">,key:string,original:PendingProtocol) {
  const current=readProtocolSave(storage,key);if(!current)return;
  if(JSON.stringify(current.body)!==JSON.stringify(original.body))throw new Error("本机已有另一项保存请求，未清除新记录。");
  removeDurably(storage,key);
}
export const protocolNumber=(value:number|null,percent=false)=>value===null?"未输出":percent?`${(value*100).toFixed(2)}%`:String(Number(value.toPrecision(8)));
