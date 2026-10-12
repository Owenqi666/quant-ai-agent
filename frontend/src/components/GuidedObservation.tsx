import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { describeError, formatDate, statusName } from "../domain";
import type { AssessmentDimensions, ObservationContext, ObservationOutputs, ObservationTemplate } from "../generated/api-contract";
import { assessmentDimensions } from "../researchAssessment";
import type { Dimension } from "../researchAssessment";
import { beginGuidedSession, buildGuidedObservation, changeGuidedTimer, finishGuidedSession, guidedKey, initialGuidedDraft, interruptGuidedSession, loadGuidedDraft, safeSessionId, saveGuidedDraft } from "../guidedObservation";
import type { GuidedDraft, GuidedSettings, IntervalKind, Phase } from "../guidedObservation";
import { observationPhases, ObservationRequestEpoch } from "../workflowObservations";
import { Field, JsonDetails } from "./ui";

interface Props {
  workspaceId:string;researchId:string;revisionId?:string;visible:boolean;onOpen?:()=>void;
  onRecordingChange?:(recording:boolean)=>void;blocked:boolean;submittedGuided?:{sessionId:string;snapshot:string;clearedRaw:string|null};
  onReady:(observation:Record<string,unknown>,snapshot:string)=>Promise<void>;onInvalidate:()=>void;
}
export default function GuidedObservation({workspaceId,researchId,revisionId,visible,onOpen,onRecordingChange,blocked,submittedGuided,onReady,onInvalidate}:Props) {
  const key=guidedKey(workspaceId,researchId);
  const [draft,setDraft]=useState<GuidedDraft>(initialGuidedDraft);
  const current=useRef(draft), loaded=useRef(false), mounted=useRef(true);
  const durableRaw=useRef<string|null>(null), restoredDraft=useRef(false), metadataTouched=useRef(false), metadataApplied=useRef(false);
  const [ready,setReady]=useState(false),[unreadable,setUnreadable]=useState(false),[error,setError]=useState("");
  const [template,setTemplate]=useState<ObservationTemplate|null>(null),[context,setContext]=useState<ObservationContext|null>(null);
  const [contextError,setContextError]=useState(""),[loading,setLoading]=useState(false),[preparing,setPreparing]=useState(false),[notice,setNotice]=useState("");
  const contextEpoch=useRef(new ObservationRequestEpoch());
  const session=draft.session, recording=!!session&&!session.finishedAt;
  const frozenRevision=session?.revisionId||revisionId;
  function durableWrite(next:GuidedDraft){
    if(localStorage.getItem(key)!==durableRaw.current)throw new Error("另一页面已修改流程草稿，请刷新后恢复最新记录；没有覆盖其他页面的数据。");
    saveGuidedDraft(localStorage,key,next);durableRaw.current=localStorage.getItem(key);
  }
  function persist(next:GuidedDraft) {
    durableWrite(next);
    current.current=next;setDraft(next);setError("");setNotice("");onInvalidate();
  }
  function update(next:GuidedDraft){try{persist(next);}catch(e){setError(`草稿未保存，操作未应用。${describeError(e)}`);}}
  useEffect(()=>{
    mounted.current=true;
    try {durableRaw.current=localStorage.getItem(key);const stored=loadGuidedDraft(localStorage,key);if(stored){restoredDraft.current=true;durableWrite(stored);current.current=stored;setDraft(stored);} loaded.current=true;}
    catch(e){setError(describeError(e));setUnreadable(true);}
    setReady(true);
    void api<ObservationTemplate>("/workflow-observation-template").then(value=>{if(mounted.current)setTemplate(value);}).catch(e=>{if(mounted.current)setContextError(describeError(e));});
    return ()=>{
      mounted.current=false;contextEpoch.current.invalidate();
      // Preserve an unknown endpoint on actual research/workspace changes. Reload recovery also covers process loss.
      if(loaded.current&&current.current.session?.open){
        try{const interrupted=interruptGuidedSession(current.current);durableWrite(interrupted);current.current=interrupted;}
        catch{/* The already-durable open interval is converted to interrupted on the next recovery. */}
      }
    };
  // The outer component is keyed by workspace + research; hidden navigation never unmounts it.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[key]);
  useEffect(()=>{onRecordingChange?.(recording);return()=>onRecordingChange?.(false);},[recording,onRecordingChange]);
  useEffect(()=>{
    if(!submittedGuided||current.current.session?.id!==submittedGuided.sessionId)return;
    if(JSON.stringify(current.current)!==submittedGuided.snapshot||!submittedGuided.clearedRaw){setNotice("原观测已保存；本机更新后的草稿已保留，没有用历史提交清除新修改。");return;}
    try{
      if(localStorage.getItem(key)!==submittedGuided.clearedRaw)throw new Error("另一页面已产生新草稿，请刷新后恢复；未覆盖新记录。");
      const next=loadGuidedDraft(localStorage,key);if(!next)throw new Error("已确认的本机草稿不存在。");
      durableRaw.current=submittedGuided.clearedRaw;current.current=next;setDraft(next);setNotice("记录已保存。共同条件已保留，可开始另一条路径；如实际环境不同，请修改后如实记录。");}
    catch(e){setError(`服务已保存，本机草稿清理失败，请保留数据。${describeError(e)}`);}
  },[submittedGuided,key]);
  async function refreshContext(runId=current.current.selectedRun,attemptId=current.current.selectedAttempt) {
    const token=contextEpoch.current.begin();setLoading(true);setContextError("");onInvalidate();
    const params=new URLSearchParams();if(frozenRevision)params.set("revision_id",frozenRevision);if(runId)params.set("run_id",runId);if(attemptId)params.set("attempt_id",attemptId);
    try{const value=await api<ObservationContext>(`/researches/${encodeURIComponent(researchId)}/workflow-observation-context?${params}`);
      if(mounted.current&&contextEpoch.current.isCurrent(token)){
        setContext(value);
        if(!restoredDraft.current&&!metadataTouched.current&&!metadataApplied.current&&!current.current.session){
          metadataApplied.current=true;
          const settings=current.current.settings;
          if(!settings.codeCommit&&Object.values(settings.environment).every(v=>!v)){
            const next={...current.current,settings:{...settings,environment:{...value.runtime.environment},codeCommit:value.runtime.code_commit_verified&&value.runtime.code_commit?value.runtime.code_commit:""}};
            try{persist(next);}catch(e){setError(describeError(e));}
          }
        }
      }
    }catch(e){if(mounted.current&&contextEpoch.current.isCurrent(token)){setContext(null);setContextError(describeError(e));}}
    finally{if(mounted.current&&contextEpoch.current.isCurrent(token))setLoading(false);}
  }
  useEffect(()=>{if(ready&&loaded.current)void refreshContext();
  // Explicit result selections trigger refresh below; revision changes may only update a not-yet-started session.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[ready,frozenRevision,researchId]);
  function settings(next:Partial<GuidedSettings>){if(next.environment||next.codeCommit!==undefined)metadataTouched.current=true;update({...draft,settings:{...draft.settings,...next}});}
  function begin(){try{
    if(!context?.compatibility.compatible||!template)throw new Error("请先加载兼容的固定对照任务。");
    const protocol=context.protocol;
    persist(beginGuidedSession(draft,{id:protocol.id,digest:protocol.digest,taskSha256:protocol.task_sha256},context.revision_id,new Date().toISOString(),crypto.randomUUID()));
  }catch(e){setError(describeError(e));}}
  function timer(phase:Phase,kind:IntervalKind|null){if(!session)return;try{persist({...draft,session:changeGuidedTimer(session,new Date().toISOString(),phase,kind)});}catch(e){setError(describeError(e));}}
  function finish(){if(!session)return;try{persist({...draft,session:finishGuidedSession(session,new Date().toISOString())});}catch(e){setError(describeError(e));}}
  async function prepare(){
    setPreparing(true);setError("");
    try{
      if(!session||!template)throw new Error("模板尚未加载。");
      const manual=session.settings.condition==="manual_cli";
      let dimensions:AssessmentDimensions|null=null;
      if(manual){
        const values=Object.values(draft.dimensions);
        if(values.some(x=>x.outcome!=="not_assessed"||x.reason.trim())){
          if(values.some(x=>!x.reason.trim()))throw new Error("已填写部分五维判断，请为每一项说明理由；未评定也需说明原因。");
          dimensions=draft.dimensions;
        }
      }
      let outputs:ObservationOutputs={task_sha256:session.protocol.taskSha256,revision_id:manual?null:session.revisionId,run_id:null,attempt_id:null,review_ids:[],
        external_run_reference:manual?(draft.externalRun.trim()||null):null,verification_evidence:draft.verification.trim()||null,report_reference:draft.report.trim()||null};
      if(!manual&&draft.selectedRun){
        const selected=context?.selected;
        if(loading||!selected||selected.run_id!==draft.selectedRun||selected.attempt_id!==(draft.selectedAttempt||null))throw new Error("请等待所选实验版本加载完成，或刷新实验列表。");
        if(selected.attempt_id&&selected.attempts.some(a=>a.id===selected.attempt_id&&!["completed","failed","cancelled","interrupted"].includes(a.status)))
          throw new Error("所选执行尝试仍在运行。请等待结束，或明确选择“尚未绑定尝试”保存未完成记录。");
        outputs={...selected.bound_outputs,report_reference:draft.report.trim()||selected.bound_outputs.report_reference,review_ids:[]};
        if(draft.selectedReview){
          const review=selected.reviews.find(x=>x.id===draft.selectedReview&&x.source==="human"&&x.reviewer===session.settings.participant);
          if(!review)throw new Error("所选审核不属于本人的该次结果，请重新选择。");
          dimensions=review.dimensions;outputs.review_ids=[review.id];
        }
      }
      if(draft.completion==="completed"){
        if(!manual&&(!outputs.attempt_id||!outputs.review_ids.length))throw new Error("完成记录需要选择本次实验、执行尝试和本人已保存的分项审核；未完成也可以如实保存。");
        if(!dimensions||Object.values(dimensions).some(d=>d.outcome==="not_assessed"||!d.reason.trim()))throw new Error("完成记录需要五项实际判断及理由；尚未检查的项目请保留为未完成。");
        if(!outputs.verification_evidence||!outputs.report_reference||(manual&&!outputs.external_run_reference))throw new Error("请补充实际输出目录、核验依据与报告位置后标为完成，或保存为未完成。");
      }
      const observation=buildGuidedObservation(draft,template.observation,outputs,dimensions);
      await onReady(observation as unknown as Record<string,unknown>,JSON.stringify(draft));
    }catch(e){setError(describeError(e));}
    finally{if(mounted.current)setPreparing(false);}
  }
  const disabled=!ready||unreadable||blocked||preparing;
  const effective=session?.settings||draft.settings;
  const compact=recording?<section className="info-note" aria-label="正在记录流程">
    <p>流程记录：{effective.condition==="workbench"?"工作台":"手动 CLI"} · {observationPhases[session.phase]} · {session.interrupted?"已中断，请保存未完成记录":session.open?({active:"主动计时中",waiting:"等待计时中",away:"离开计时中"}[session.open.kind]):"已暂停"}</p>
    <Field label="当前记录阶段"><select disabled={disabled||session.interrupted} value={session.phase} onChange={e=>timer(e.target.value as Phase,session.open?"active":null)}>{Object.entries(observationPhases).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></Field>
    <div className="button-row"><button className="button small" disabled={disabled||!session.open} onClick={()=>timer(session.phase,null)}>暂停流程计时</button><button className="button small" disabled={disabled||session.interrupted||session.open?.kind==="active"} onClick={()=>timer(session.phase,"active")}>开始主动计时</button><button className="button small" disabled={disabled||session.interrupted||session.open?.kind==="waiting"} onClick={()=>timer(session.phase,"waiting")}>等待工具</button><button className="button small" disabled={disabled||session.interrupted||session.open?.kind==="away"} onClick={()=>timer(session.phase,"away")}>暂时离开</button><button className="button small" onClick={onOpen}>返回流程记录</button></div>
    {error&&<p role="alert">{error}</p>}
  </section>:null;
  if(!visible)return compact;
  return <section className="panel" aria-label="引导流程记录">
    <h2>开始流程记录</h2><p>这是可选的使用流程对照。日常研究可直接运行、审核和查看报告，无需填写本页。</p>
    {error&&<p role="alert">{error}</p>}{contextError&&<p role="alert">{contextError}</p>}{notice&&<p role="status">{notice}</p>}
    {unreadable&&<p>本机草稿无法安全恢复，已保留原数据并阻止覆盖。请导出浏览器存储后处理。</p>}
    {!session?<>
      <fieldset disabled={disabled||loading}>
        <div className="form-columns"><Field label="参与者标识"><input value={draft.settings.participant} maxLength={120} onChange={e=>settings({participant:e.target.value})} placeholder="例如 owen，需与本人审核一致"/></Field>
          <Field label="本次路径"><select value={draft.settings.condition} onChange={e=>settings({condition:e.target.value as GuidedSettings["condition"]})}><option value="workbench">工作台</option><option value="manual_cli">手动 CLI</option></select></Field></div>
        <Field label="对材料与工具的熟悉程度"><textarea rows={2} maxLength={2000} value={draft.settings.familiarity} onChange={e=>settings({familiarity:e.target.value})} placeholder="例如：研究过 Alpha101，首次使用此版工作台"/></Field>
        <div className="form-columns"><Field label="执行顺序"><input type="number" min={1} max={10000} value={draft.settings.executionOrder} onChange={e=>settings({executionOrder:Number(e.target.value)})}/></Field>
          <Field label="练习记录"><input type="checkbox" checked={draft.settings.practice} onChange={e=>settings({practice:e.target.checked})}/></Field></div>
        <p className="fine-print">默认作为练习，不计入正式人工完成率。开始前可关闭练习；共同条件保存后可复用于另一条路径。</p>
        <Field label="事先约定的停止条件"><input maxLength={2000} value={draft.settings.stopCondition} onChange={e=>settings({stopCondition:e.target.value})}/></Field>
        <p className="fine-print">首次已填入服务运行环境。手动 CLI 需要使用相同环境，或展开下方如实修改；恢复草稿和共同条件不会被刷新覆盖。</p>
        <details><summary>版本、环境和允许工具（首次记录需确认）</summary>
          <p>工作台环境：{context?`${context.runtime.version} · ${context.runtime.environment.os} · Python ${context.runtime.environment.python}`:"尚未加载"}。这是服务运行环境，CLI 在另一环境执行时请如实修改。</p>
          <button type="button" className="button small" disabled={!context} onClick={()=>context&&settings({environment:{...context.runtime.environment},codeCommit:context.runtime.code_commit_verified&&context.runtime.code_commit?context.runtime.code_commit:draft.settings.codeCommit})}>采用工作台环境</button>
          <Field label="执行代码的提交标识" hint="40 位 Git 提交；若运行包未提供可核验值，需手动确认一次。"><input value={draft.settings.codeCommit} onChange={e=>settings({codeCommit:e.target.value.trim()})} maxLength={40}/></Field>
          {([ ["machine","机器说明"],["os","操作系统"],["python","Python 环境"],["dependencies","依赖环境"] ] as const).map(([field,label])=><Field label={label} key={field}><input maxLength={field==="dependencies"?2000:field==="python"?100:300} value={draft.settings.environment[field]} onChange={e=>settings({environment:{...draft.settings.environment,[field]:e.target.value}})}/></Field>)}
          <Field label="允许工具（每行一个）"><textarea rows={4} maxLength={80000} value={draft.settings.tools} onChange={e=>settings({tools:e.target.value})}/></Field>
        </details>
        {context&&!context.compatibility.compatible&&<p role="alert">当前研究不符合固定对照任务：{context.compatibility.reason}</p>}
        <button className="button primary" disabled={!template||!context?.compatibility.compatible} onClick={begin}>开始本次记录</button>
      </fieldset>
      <button className="button small" disabled={loading} onClick={()=>void refreshContext()}>刷新记录上下文</button>
    </>:<>
      <p>{effective.participant} · {effective.condition==="workbench"?"工作台":"手动 CLI"} · {effective.practice?"练习":"正式对照"} · 第 {effective.executionOrder} 次。开始于 {formatDate(session.startedAt)}。</p>
      {!session.finishedAt&&<fieldset disabled={disabled||session.interrupted}>
        <div className="button-row">{Object.entries(observationPhases).map(([phase,label])=><button key={phase} className="button small" aria-pressed={session.phase===phase} onClick={()=>timer(phase as Phase,session.open?"active":null)}>{label}</button>)}</div>
        <p>当前：{observationPhases[session.phase]}；{session.open?({active:"主动计时中",waiting:"等待计时中",away:"离开计时中"}[session.open.kind]):"已暂停"}。已结束 {session.intervals.filter(x=>x.status==="ended").length} 个区间。</p>
        <div className="button-row"><button className="button small" disabled={session.open?.kind==="active"} onClick={()=>timer(session.phase,"active")}>开始主动计时</button><button className="button small" disabled={!session.open} onClick={()=>timer(session.phase,null)}>暂停流程计时</button><button className="button small" disabled={session.open?.kind==="waiting"} onClick={()=>timer(session.phase,"waiting")}>等待工具</button><button className="button small" disabled={session.open?.kind==="away"} onClick={()=>timer(session.phase,"away")}>暂时离开</button></div>
        <p className="fine-print">切换阶段会结束原区间。离开浏览器阅读 PDF 或使用终端不会自动暂停；请按实际活动切换。切换研究、关闭或刷新时，未结束区间会标为中断。</p>
      </fieldset>}
      {session.interrupted&&<p role="alert">上次计时已中断，未知结束时间保持空白。请结束并保存未完成记录；新会话可以继续工作，历史区间不续算。</p>}
      {!session.finishedAt&&<button className="button primary" disabled={disabled} onClick={finish}>结束本次记录</button>}
      {effective.condition==="manual_cli"&&<details><summary>CLI 操作命令</summary><p>先激活与已声明环境一致的 Python 环境，在项目根目录运行。下方使用同一固定任务，输出目录按本次会话生成。</p>
        <CopyCommand sessionId={session.id}/></details>}
      {session.finishedAt&&<fieldset disabled={disabled}>
        <h3>核对本次结果</h3><p>本表单只记录本人实际操作。保存前会校验结果与实验的关联，不会替你作出研究判断。</p>
        <Field label="本次完成情况"><select value={draft.completion} onChange={e=>update({...draft,completion:e.target.value as GuidedDraft["completion"]})}><option value="incomplete">未完成</option><option value="completed" disabled={session.interrupted}>已完成</option><option value="abandoned">已放弃</option></select></Field>
        {draft.completion!=="completed"&&<Field label="未完成或放弃原因"><textarea value={draft.reason} onChange={e=>update({...draft,reason:e.target.value})} rows={2}/></Field>}
        {effective.condition==="workbench"?<>
          <Field label="本次实验"><select disabled={loading} value={draft.selectedRun} onChange={e=>{update({...draft,selectedRun:e.target.value,selectedAttempt:"",selectedReview:""});setContext(null);void refreshContext(e.target.value,"");}}><option value="">尚未运行 / 不绑定实验</option>{context?.runs.filter(run=>run.revision_id===frozenRevision&&run.mode==="normalized_fixed").map(run=><option key={run.id} value={run.id}>{formatDate(run.created_at)} · {statusName(run.status)} · {run.id.slice(0,8)}</option>)}</select></Field>
          <button className="button small" disabled={loading} onClick={()=>void refreshContext()}>刷新实验与审核</button>
          {context?.runs_truncated&&<p>从最近 20 次实验中列出同修订、同执行方式的记录；完整性将在选择执行尝试后检查。更早记录仍可用高级导入。</p>}
          {context?.selected&&<><Field label="本次执行尝试"><select disabled={loading} value={draft.selectedAttempt} onChange={e=>{update({...draft,selectedAttempt:e.target.value,selectedReview:""});void refreshContext(draft.selectedRun,e.target.value);}}><option value="">尚未绑定尝试 / 请选择实际使用的尝试</option>{context.selected.attempts.map(a=><option key={a.id} value={a.id}>第 {a.number} 次 · {statusName(a.status)}</option>)}</select></Field>
            {draft.selectedAttempt&&<><p>结果完整性：{context.selected.verified?"已核验":context.selected.verification_error||"未核验"}</p>
              <Field label="本人的分项审核"><select value={draft.selectedReview} onChange={e=>update({...draft,selectedReview:e.target.value})}><option value="">选择已经保存的本人审核</option>{context.selected.reviews.filter(r=>r.source==="human"&&r.reviewer===effective.participant).map(r=><option key={r.id} value={r.id}>{r.reviewer} · {formatDate(r.created_at)} · {r.id.slice(0,8)}</option>)}</select></Field>
              <p className="fine-print">选择后直接复用该审核的五维判断，不用重复填写。只有审核人标识相同的人工审核会出现在列表；在“审核与回流”完成一次审核后刷新即可。</p>
              {draft.selectedReview&&<JsonDetails value={context.selected.reviews.find(r=>r.id===draft.selectedReview)?.dimensions} label="查看选中的五维判断"/>}
            </>}
          </>}
          <Field label="研究报告或总结引用（选填；未填时使用实验报告）"><input value={draft.report} onChange={e=>update({...draft,report:e.target.value})}/></Field>
        </>:<>
          <Field label="CLI 输出目录"><input value={draft.externalRun} onChange={e=>update({...draft,externalRun:e.target.value})}/></Field>
          <Field label="完整性核验依据"><textarea rows={2} value={draft.verification} onChange={e=>update({...draft,verification:e.target.value})} placeholder="核验文件位置及实际结论"/></Field>
          <Field label="报告或研究说明位置"><input value={draft.report} onChange={e=>update({...draft,report:e.target.value})}/></Field>
          <p className="fine-print">外部位置只保存为声明，不会触发服务读取任意本机文件。</p>
          <details open={draft.completion==="completed"}><summary>手动路径五维判断</summary>{Object.entries(assessmentDimensions).map(([key,label])=><div key={key}><Field label={`${label}判断`}><select value={draft.dimensions[key as Dimension].outcome} onChange={e=>update({...draft,dimensions:{...draft.dimensions,[key]:{...draft.dimensions[key as Dimension],outcome:e.target.value}}})}><option value="not_assessed">未评定</option><option value="passed">通过</option><option value="failed">未通过</option><option value="not_applicable">不适用</option></select></Field><Field label={`${label}理由`}><textarea rows={2} maxLength={2000} value={draft.dimensions[key as Dimension].reason} onChange={e=>update({...draft,dimensions:{...draft.dimensions,[key]:{...draft.dimensions[key as Dimension],reason:e.target.value}}})}/></Field></div>)}</details>
        </>}
        <details><summary>错误、返工和其他补充（没有则留空）</summary>{([["rework","错误与返工"],["reuse","模板复用"],["help","获得的帮助"],["limitations","适用限制与学习效应"]] as const).map(([key,label])=><Field key={key} label={label}><textarea rows={2} value={draft[key]} onChange={e=>update({...draft,[key]:e.target.value})}/></Field>)}</details>
        <button className="button primary" disabled={loading||!template} onClick={()=>void prepare()}>检查并准备保存</button>
      </fieldset>}
      <details><summary>已记录的时间区间</summary><ul>{session.intervals.map((interval,i)=><li key={i}>{observationPhases[interval.phase]} · {interval.kind} · {interval.started_at} → {interval.ended_at||"中断，结束时间未知"}</li>)}</ul><p>未出现的阶段保持未测量，不补零。</p></details>
    </>}
  </section>;
}
function CopyCommand({sessionId}:{sessionId:string}){
  const [copied,setCopied]=useState(false),[error,setError]=useState("");
  if(!safeSessionId(sessionId))return <p role="alert">会话标识无效，未生成命令。</p>;
  const directory=`artifacts/manual-${sessionId}`;
  const command=`python -m paper_alpha run evaluation_suites/v05/task.json --out ${directory} --mode normalized_fixed\npython -m paper_alpha verify ${directory}`;
  return <><pre>{command}</pre><button className="button small" onClick={()=>void navigator.clipboard.writeText(command).then(()=>{setCopied(true);setError("");}).catch(()=>setError("无法访问剪贴板，请选择上方命令复制。"))}>复制 CLI 命令</button>{copied&&<span role="status">已复制</span>}{error&&<p role="alert">{error}</p>}</>;
}
