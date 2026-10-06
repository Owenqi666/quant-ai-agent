import type { AssessmentDimensions, ObservationEnvironment, ObservationInterval, WorkflowObservation } from "./generated/api-contract";
import { assessmentDimensions, emptyAssessment } from "./researchAssessment";
import { recoveryKey, writeDurably } from "./reviewRecovery";

export type Phase = ObservationInterval["phase"];
export type IntervalKind = ObservationInterval["kind"];
export interface GuidedSettings {
  participant: string; condition: "manual_cli" | "workbench"; executionOrder: number;
  practice: boolean; familiarity: string; stopCondition: string; tools: string;
  codeCommit: string; environment: ObservationEnvironment;
}
export interface GuidedSession {
  id: string; startedAt: string; finishedAt: string | null; settings: GuidedSettings;
  phase: Phase; intervals: ObservationInterval[]; open: {phase:Phase; kind:IntervalKind; startedAt:string} | null;
  interrupted: boolean; protocol: {id:string; digest:string; taskSha256:string}; revisionId:string | null;
}
export interface GuidedDraft {
  version: 1; settings: GuidedSettings; session: GuidedSession | null;
  completion: "completed" | "incomplete" | "abandoned"; reason: string;
  selectedRun: string; selectedAttempt: string; selectedReview: string;
  externalRun: string; verification: string; report: string; dimensions: AssessmentDimensions;
  rework: string; reuse: string; help: string; limitations: string;
}
export const guidedKey = (workspace:string, research:string) => recoveryKey(workspace,"guided-observation",research);
export const safeSessionId = (value:string) => /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
export function initialGuidedDraft(): GuidedDraft {
  return {version:1, settings:{participant:"", condition:"workbench", executionOrder:1, practice:true,
    familiarity:"", stopCondition:"完成研究与报告，或主动工作达到 45 分钟后停止。", tools:"论文 PDF\n文本编辑器\n本地 Python CLI\n工作台",
    codeCommit:"", environment:{machine:"",os:"",python:"",dependencies:""}}, session:null,
    completion:"incomplete", reason:"", selectedRun:"", selectedAttempt:"", selectedReview:"", externalRun:"", verification:"", report:"",
    dimensions:emptyAssessment().dimensions, rework:"",reuse:"",help:"",limitations:""};
}
export const textLines = (value:string) => value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
function copy<T>(value:T):T {return JSON.parse(JSON.stringify(value)) as T;}
export function validateSettings(s:GuidedSettings) {
  if(!validSettings(s))throw new Error("记录设置格式或长度超出限制，请核对输入。");
  if (!s.participant.trim() || !s.familiarity.trim() || !s.stopCondition.trim() || !textLines(s.tools).length)
    throw new Error("请填写参与者、熟悉程度、停止条件和允许工具。");
  if (!Number.isInteger(s.executionOrder) || s.executionOrder<1 || s.executionOrder>10000) throw new Error("执行顺序需要是 1 到 10000 的整数。");
  if (!/^[0-9a-f]{40}$/.test(s.codeCommit)) throw new Error("请在版本与环境中确认 40 位代码提交标识；系统不会猜测运行版本。");
  if (Object.values(s.environment).some(x=>!x.trim())) throw new Error("请确认版本与环境中的机器、系统、Python 和依赖说明。");
}
export function beginGuidedSession(draft:GuidedDraft, protocol:GuidedSession["protocol"], revisionId:string|null,
  now:string, id:string):GuidedDraft {
  if (draft.session) throw new Error("请先完成或保存当前记录。");
  if(!safeSessionId(id)||!timestamp(now))throw new Error("会话标识或开始时间无效。");
  validateSettings(draft.settings);
  return {...draft, session:{id,startedAt:now,finishedAt:null,settings:copy({...draft.settings,participant:draft.settings.participant.trim(),familiarity:draft.settings.familiarity.trim(),stopCondition:draft.settings.stopCondition.trim()}),
    phase:"reading",intervals:[],open:{phase:"reading",kind:"active",startedAt:now},interrupted:false,protocol:copy(protocol),revisionId},
    completion:"incomplete",reason:"",selectedRun:"",selectedAttempt:"",selectedReview:"",externalRun:"",verification:"",report:"",
    dimensions:emptyAssessment().dimensions,rework:"",reuse:"",help:"",limitations:""};
}
/** Close exactly one observed interval before changing phase/kind. A pause creates no inferred time. */
export function changeGuidedTimer(session:GuidedSession, now:string, phase:Phase, kind:IntervalKind|null):GuidedSession {
  if(session.finishedAt || session.interrupted) throw new Error("已结束或中断的会话不能继续计时，请保存后新建记录。");
  const intervals=[...session.intervals];
  if(session.open) {
    if(Date.parse(now)<=Date.parse(session.open.startedAt)) throw new Error("时间未向前推进，请检查本机时钟后重试。");
    intervals.push({phase:session.open.phase,kind:session.open.kind,started_at:session.open.startedAt,ended_at:now,status:"ended",source_reference:null});
  }
  if(intervals.length+(kind?1:0)>300) throw new Error("已达到 300 个区间上限，请结束当前记录。");
  const previous=intervals.at(-1);
  if(previous?.ended_at && Date.parse(now)<Date.parse(previous.ended_at)) throw new Error("本机时钟早于已保存区间，请检查时间。");
  return {...session,phase,intervals,open:kind?{phase,kind,startedAt:now}:null};
}
export function finishGuidedSession(session:GuidedSession, now:string):GuidedSession {
  if(session.finishedAt) return session;
  if(Date.parse(now)<Date.parse(session.startedAt)) throw new Error("结束时间早于会话开始，请检查本机时钟。");
  const stopped=session.interrupted?session:changeGuidedTimer(session,now,session.phase,null);
  return {...stopped,finishedAt:now};
}
/** An unknown end is never reconstructed. Existing backend treats it as occupying the rest of this session. */
export function interruptGuidedSession(draft:GuidedDraft):GuidedDraft {
  const session=draft.session;
  if(!session?.open) return draft;
  const open=session.open;
  return {...draft,completion:"incomplete",session:{...session,open:null,interrupted:true,
    intervals:[...session.intervals,{phase:open.phase,kind:open.kind,started_at:open.startedAt,ended_at:null,status:"interrupted",source_reference:null}]}};
}
function isObject(value:unknown):value is Record<string,unknown>{return !!value&&typeof value==="object"&&!Array.isArray(value);}
const phases=["reading","hypothesis","implementation","run_setup","review","report"];
const kinds=["active","waiting","away"];
const bounded=(v:unknown,max=4000):v is string=>typeof v==="string"&&v.length<=max;
function timestamp(value:unknown):value is string{return typeof value==="string"&&value.length<=40&&/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(value)&&Number.isFinite(Date.parse(value));}
function exact(v:Record<string,unknown>,keys:string[]){return Object.keys(v).length===keys.length&&keys.every(k=>Object.hasOwn(v,k));}
function validSettings(v:unknown):v is GuidedSettings {
  if(!isObject(v)||!exact(v,["participant","condition","executionOrder","practice","familiarity","stopCondition","tools","codeCommit","environment"])||
    !bounded(v.participant,120)||!bounded(v.familiarity,2000)||!bounded(v.stopCondition,2000)||!bounded(v.tools,80000)||
    !bounded(v.codeCommit,40)||
    !["manual_cli","workbench"].includes(String(v.condition))||typeof v.practice!=="boolean"||
    typeof v.executionOrder!=="number"||!Number.isInteger(v.executionOrder)||v.executionOrder<0||v.executionOrder>10000||!isObject(v.environment))return false;
  return exact(v.environment,["machine","os","python","dependencies"])&&bounded(v.environment.machine,300)&&bounded(v.environment.os,300)&&bounded(v.environment.python,100)&&bounded(v.environment.dependencies,2000);
}
function validDimensions(v:unknown):v is AssessmentDimensions {
  return isObject(v)&&exact(v,Object.keys(assessmentDimensions))&&Object.values(v).every(d=>isObject(d)&&exact(d,["outcome","reason"])&&
    ["passed","failed","not_assessed","not_applicable"].includes(String(d.outcome))&&bounded(d.reason,2000));
}
function validSession(v:unknown):v is GuidedSession {
  if(!isObject(v)||!exact(v,["id","startedAt","finishedAt","settings","phase","intervals","open","interrupted","protocol","revisionId"])||
    !bounded(v.id,36)||!safeSessionId(v.id)||!timestamp(v.startedAt)||(v.finishedAt!==null&&!timestamp(v.finishedAt))||
    !validSettings(v.settings)||!phases.includes(String(v.phase))||typeof v.interrupted!=="boolean"||
    !(v.revisionId===null||bounded(v.revisionId,64))||!isObject(v.protocol)||!exact(v.protocol,["id","digest","taskSha256"])||
    !bounded(v.protocol.id,120)||!v.protocol.id.trim()||!bounded(v.protocol.digest,64)||!/^[0-9a-f]{64}$/.test(v.protocol.digest)||
    !bounded(v.protocol.taskSha256,64)||!/^[0-9a-f]{64}$/.test(v.protocol.taskSha256)||!Array.isArray(v.intervals)||v.intervals.length>300)return false;
  try{validateSettings(v.settings);}catch{return false;}
  let previousEnd=Date.parse(v.startedAt), interrupted=false;
  for(const i of v.intervals){
    if(!isObject(i)||!exact(i,["phase","kind","started_at","ended_at","status","source_reference"])||!phases.includes(String(i.phase))||!kinds.includes(String(i.kind))||!timestamp(i.started_at)||
      i.source_reference!==null||Date.parse(i.started_at)<previousEnd||interrupted)return false;
    if(i.status==="ended"&&timestamp(i.ended_at)&&Date.parse(i.ended_at)>Date.parse(i.started_at))previousEnd=Date.parse(i.ended_at);
    else if(i.status==="interrupted"&&i.ended_at===null){interrupted=true;previousEnd=Date.parse(i.started_at);}
    else return false;
  }
  if(interrupted!==v.interrupted)return false;
  if(v.open!==null){
    if(interrupted||v.finishedAt!==null||v.intervals.length>=300||!isObject(v.open)||!exact(v.open,["phase","kind","startedAt"])||!phases.includes(String(v.open.phase))||!kinds.includes(String(v.open.kind))||
      !timestamp(v.open.startedAt)||Date.parse(v.open.startedAt)<previousEnd)return false;
  }
  if(v.finishedAt!==null&&Date.parse(v.finishedAt as string)<previousEnd)return false;
  return true;
}
export function saveGuidedDraft(storage:Pick<Storage,"setItem"|"getItem">,key:string,draft:GuidedDraft){
  // Validate the exact persistence envelope before writing. Partial setup text may be unfinished;
  // frozen session settings and interval semantics must already be valid and recoverable.
  const raw=JSON.stringify({version:1,scope:key,draft});
  loadGuidedDraft({getItem:()=>raw},key);
  writeDurably(storage,key,{version:1,scope:key,draft});
}
export function nextGuidedDraft(previous:GuidedDraft):GuidedDraft {
  return {...initialGuidedDraft(),settings:{...previous.settings,condition:previous.settings.condition==="workbench"?"manual_cli":"workbench",executionOrder:previous.settings.executionOrder+1}};
}
/** Called inside pending-request acknowledgement, before deleting the recovery receipt. */
export function completeGuidedSubmission(storage:Pick<Storage,"getItem"|"setItem">,key:string,sessionId:string,snapshot:string):string|null {
  const originalRaw=storage.getItem(key);if(originalRaw===null)return null;
  const original=loadGuidedDraft(storage,key);
  if(!original||original.session?.id!==sessionId||JSON.stringify(original)!==snapshot)return null;
  if(storage.getItem(key)!==originalRaw)throw new Error("流程草稿已被另一页面修改，请安全重试确认。");
  saveGuidedDraft(storage,key,nextGuidedDraft(original));
  return storage.getItem(key);
}
export function loadGuidedDraft(storage:Pick<Storage,"getItem">,key:string):GuidedDraft|null {
  const raw=storage.getItem(key); if(raw===null)return null;
  if(new TextEncoder().encode(raw).length>512*1024)throw new Error("流程草稿过大，未自动应用，请先导出保留。");
  const value:unknown=JSON.parse(raw);
  if(!isObject(value)||value.version!==1||value.scope!==key||!isObject(value.draft)) throw new Error("流程草稿格式或作用域不匹配，请先导出保留。");
  const d=value.draft;
  if(!exact(d,Object.keys(initialGuidedDraft()))||d.version!==1||!validSettings(d.settings)||
    !["completed","incomplete","abandoned"].includes(String(d.completion))||
    !["reason","externalRun","verification","report","rework","reuse","help","limitations"].every(k=>bounded(d[k],200000))||
    !["selectedRun","selectedAttempt","selectedReview"].every(k=>bounded(d[k],64))||
    !validDimensions(d.dimensions)||!(d.session===null||validSession(d.session)))
    throw new Error("流程草稿不完整，未自动应用，请导出保留。");
  return interruptGuidedSession(d as unknown as GuidedDraft);
}
export function buildGuidedObservation(draft:GuidedDraft, template:Record<string,unknown>, outputs:WorkflowObservation["bound_outputs"], dimensions:AssessmentDimensions|null):WorkflowObservation {
  const s=draft.session;
  if(!s?.finishedAt) throw new Error("请先结束本次记录。");
  if(s.interrupted&&draft.completion==="completed") throw new Error("计时中断的会话请保存为未完成或已放弃。");
  if(draft.completion!=="completed"&&!draft.reason.trim()) throw new Error("请简述未完成或放弃的原因。");
  const settings=s.settings;
  return {...template,schema_version:1,protocol_id:s.protocol.id,protocol_digest:s.protocol.digest,record_status:"observed",source:"human",
    participant:settings.participant,session_id:s.id,condition:settings.condition,execution_order:settings.executionOrder,
    code_commit:settings.codeCommit,environment:copy(settings.environment),allowed_tools:textLines(settings.tools),prior_familiarity:settings.familiarity,
    practice_session:settings.practice,predeclared_stop_condition:settings.stopCondition,started_at:s.startedAt,finished_at:s.finishedAt,
    completion:draft.completion,incomplete_reason:draft.completion==="completed"?null:draft.reason.trim(),intervals:copy(s.intervals),
    errors_and_rework:textLines(draft.rework),template_reuse:textLines(draft.reuse),help_received:textLines(draft.help),bound_outputs:outputs,
    dimensions,limitations:textLines(draft.limitations)} as WorkflowObservation;
}
