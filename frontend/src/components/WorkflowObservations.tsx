import { useEffect, useRef, useState } from "react";
import { api, ApiError, post } from "../api";
import { describeError, formatDate, shortId } from "../domain";
import type { ObservationDetail, ObservationList, ObservationSummary, ObservationTemplate, ObservationTiming, ObservationValidation } from "../generated/api-contract";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { clearSubmittedObservationDraft, loadObservationDraft, MAX_OBSERVATION_BYTES, observationPath, observationPhases, observationScopeKey, observationSources, ObservationRequestEpoch, observedSeconds, parseObservation, saveObservationDraft } from "../workflowObservations";
import type { ObservationDraft, ObservationSource } from "../workflowObservations";
import { Field, JsonDetails } from "./ui";
import GuidedObservation from "./GuidedObservation";
import { completeGuidedSubmission, guidedKey } from "../guidedObservation";

export interface WorkflowObservationsProps { workspaceId: string; researchId: string; revisionId?: string; visible?: boolean; onOpen?:()=>void; onRecordingChange?:(recording:boolean)=>void }

/** A new selection gets new state and request epochs even if the parent omits its own key. */
export default function WorkflowObservations(props: WorkflowObservationsProps) {
  if (!props.workspaceId || !props.researchId) return <section className="panel"><p>请连接工作区并选择研究后记录流程观测。</p></section>;
  return <ObservationWorkspace key={JSON.stringify([props.workspaceId, props.researchId])} {...props} />;
}

function downloadJson(value: unknown, name: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + "\n"], {type:"application/json"}));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  // Keep the object alive until the browser has consumed the download event.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ObservationWorkspace({ workspaceId, researchId, revisionId, visible=true, onOpen, onRecordingChange }: WorkflowObservationsProps) {
  const path = observationPath(researchId), draftKey = observationScopeKey(workspaceId, researchId, "draft");
  const mutation = useRecoverableMutation<ObservationDetail>(observationScopeKey(workspaceId, researchId, "import"), path);
  const [draft, setDraft] = useState<ObservationDraft>({raw:"", source:""});
  const [draftReady, setDraftReady] = useState(false);
  const [draftBlocked, setDraftBlocked] = useState(false);
  const [draftError, setDraftError] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [submittedGuided, setSubmittedGuided] = useState<{sessionId:string;snapshot:string;clearedRaw:string|null}>();
  const [guidedValidation, setGuidedValidation] = useState(false);
  const [validation, setValidation] = useState<{draft:ObservationDraft; value:ObservationValidation; observation:Record<string,unknown>;guidedSnapshot?:string} | null>(null);
  const [list, setList] = useState<ObservationDetail[]>([]);
  const [total, setTotal] = useState(0);
  const [nextOffset, setNextOffset] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const pageSizeRef = useRef(20);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [detail, setDetail] = useState<ObservationDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState("");
  const [detailError, setDetailError] = useState("");
  const [summary, setSummary] = useState<ObservationSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [summaryError, setSummaryError] = useState("");
  const mounted = useRef(true);
  const fileEpoch = useRef(new ObservationRequestEpoch());
  const validationEpoch = useRef(new ObservationRequestEpoch());
  const listEpoch = useRef(new ObservationRequestEpoch());
  const detailEpoch = useRef(new ObservationRequestEpoch());
  const summaryEpoch = useRef(new ObservationRequestEpoch());

  useEffect(() => {
    mounted.current = true;
    try { const saved = loadObservationDraft(localStorage, draftKey); if (saved) setDraft(saved); }
    catch (e) { setDraftError(`无法读取原观测草稿。${describeError(e)}`); setDraftBlocked(true); }
    setDraftReady(true);
    void loadList(0).catch(() => undefined);
    return () => {
      mounted.current = false;
      for (const scope of [fileEpoch, validationEpoch, listEpoch, detailEpoch, summaryEpoch]) scope.current.invalidate();
    };
    // This component is remounted for every immutable workspace/research scope.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function edit(next: ObservationDraft) {
    fileEpoch.current.invalidate(); validationEpoch.current.invalidate();
    setBusy((current) => current === "validate" ? "" : current);
    setValidation(null); setGuidedValidation(false); setConfirmed(false); setError(""); setNotice(""); setDraft(next);
    try { saveObservationDraft(localStorage, draftKey, next); setDraftError(""); }
    catch (e) { setDraftError(`草稿未能保存到本机，请先下载备份。${describeError(e)}`); }
  }
  async function upload(file: File | undefined) {
    if (!file) return;
    const token = fileEpoch.current.begin();
    setError("");
    try {
      if (!file.name.toLowerCase().endsWith(".json") || file.size <= 0 || file.size > MAX_OBSERVATION_BYTES)
        throw new Error("请选择非空且不超过 256 KiB 的 .json 文件。");
      const raw = await file.text();
      if (mounted.current && fileEpoch.current.isCurrent(token)) edit({raw,source:""});
    } catch (e) { if (mounted.current && fileEpoch.current.isCurrent(token)) setError(describeError(e)); }
  }
  async function template() {
    setBusy("template"); setError("");
    try {
      const value = await api<ObservationTemplate>("/workflow-observation-template");
      if (!mounted.current) return;
      downloadJson(value.observation, "workflow-observation-blank.json");
      setNotice(`空白模板已准备下载。协议 ${value.protocol.id}，摘要 ${value.protocol.digest}；未填写模板不能导入。`);
    } catch (e) { if (mounted.current) setError(describeError(e)); }
    finally { if (mounted.current) setBusy(""); }
  }
  async function validate() {
    const token = validationEpoch.current.begin();
    setBusy("validate"); setError(""); setValidation(null); setConfirmed(false);
    try {
      const observation = parseObservation(draft.raw, draft.source);
      const value = await post<ObservationValidation>(`${path}/validate`, {observation});
      if (mounted.current && validationEpoch.current.isCurrent(token)) setValidation({draft:{...draft}, value, observation});
    } catch (e) { if (mounted.current && validationEpoch.current.isCurrent(token)) setError(describeError(e)); }
    finally { if (mounted.current && validationEpoch.current.isCurrent(token)) setBusy(""); }
  }
  function invalidateGuided() {
    if (!guidedValidation) return;
    validationEpoch.current.invalidate(); setValidation(null); setConfirmed(false); setGuidedValidation(false);
  }
  async function prepareGuided(observation: Record<string,unknown>, guidedSnapshot:string) {
    const next:ObservationDraft={raw:JSON.stringify(observation),source:"human"};
    edit(next); setGuidedValidation(true);
    const token=validationEpoch.current.begin(); setBusy("validate");
    try {
      const value=await post<ObservationValidation>(`${path}/validate`,{observation});
      if (mounted.current&&validationEpoch.current.isCurrent(token))setValidation({draft:next,value,observation,guidedSnapshot});
    } catch(e) {if(mounted.current&&validationEpoch.current.isCurrent(token))setError(describeError(e));}
    finally {if(mounted.current&&validationEpoch.current.isCurrent(token))setBusy("");}
  }
  async function save(replay = false) {
    if (!replay && (!validation || !confirmed || validation.draft.raw !== draft.raw || validation.draft.source !== draft.source)) return;
    const originalDraft = replay ? mutation.pending?.context?.draft as ObservationDraft | undefined : validation?.draft;
    const originalGuided = replay ? mutation.pending?.context?.guidedSnapshot : validation?.guidedSnapshot;
    setBusy("save"); setError(""); setNotice("");
    try {
      const saved = await mutation.execute(replay ? undefined : {observation:validation!.observation}, replay ? undefined : {draft:originalDraft,...(typeof originalGuided==="string"?{guidedSnapshot:originalGuided}:{})});
      let localWarning = "";
      let clearedGuidedRaw:string|null=null;
      try {
        mutation.acknowledge(() => {
          if (originalDraft) clearSubmittedObservationDraft(localStorage, draftKey, originalDraft);
          if(typeof originalGuided==="string")clearedGuidedRaw=completeGuidedSubmission(localStorage,guidedKey(workspaceId,researchId),saved.observation.session_id,originalGuided);
        });
        if (mounted.current) {
          // Preserve a newer draft left by another tab even when the original POST finishes late.
          const remaining = loadObservationDraft(localStorage, draftKey);
          setDraft(remaining || {raw:"",source:""}); setValidation(null); setConfirmed(false);
        }
      } catch (e) { localWarning = `本机原请求尚未清理，请安全重试完成确认。${describeError(e)}`; }
      if (!mounted.current) return;
      if(typeof originalGuided==="string")setSubmittedGuided({sessionId:saved.observation.session_id,snapshot:originalGuided,clearedRaw:clearedGuidedRaw});
      setGuidedValidation(false);
      detailEpoch.current.invalidate(); setDetailLoading(""); setDetailError(""); setDetail(saved);
      summaryEpoch.current.invalidate(); setSummaryLoading(false); setSummary(null);
      setNotice(`观测已保存为不可变记录 ${saved.id}。${localWarning}`);
      try { await loadList(0); }
      catch (e) { if (mounted.current) setNotice(`观测已保存，列表刷新暂未成功；已保存详情和导出仍可使用。${describeError(e)}${localWarning}`); }
    } catch (e) { if (mounted.current) setError(describeError(e)); }
    finally { if (mounted.current) setBusy(""); }
  }
  async function loadList(offset: number) {
    const token = listEpoch.current.begin();
    if (mounted.current) { setListLoading(true); setListError(""); }
    try {
      const value = await api<ObservationList>(`${path}?limit=${pageSizeRef.current}&offset=${offset}`);
      if (!mounted.current || !listEpoch.current.isCurrent(token)) return;
      setList((previous) => offset ? [...new Map([...previous,...value.items].map((row) => [row.id,row])).values()] : value.items);
      setTotal(value.total); setNextOffset(value.offset + value.items.length);
    } catch (e) {
      if (mounted.current && listEpoch.current.isCurrent(token)) setListError(e instanceof ApiError && e.status === 413 ? `本页观测超过读取大小限制，请减少每页数量后重试。${describeError(e)}` : describeError(e));
      throw e;
    } finally { if (mounted.current && listEpoch.current.isCurrent(token)) setListLoading(false); }
  }
  async function showDetail(id: string) {
    const token = detailEpoch.current.begin(); setDetail(null); setDetailLoading(id); setDetailError("");
    try {
      const value = await api<ObservationDetail>(`${path}/${encodeURIComponent(id)}`);
      if (mounted.current && detailEpoch.current.isCurrent(token)) setDetail(value);
    } catch (e) { if (mounted.current && detailEpoch.current.isCurrent(token)) setDetailError(describeError(e)); }
    finally { if (mounted.current && detailEpoch.current.isCurrent(token)) setDetailLoading(""); }
  }
  async function loadSummary() {
    const token = summaryEpoch.current.begin(); setSummary(null); setSummaryError(""); setSummaryLoading(true);
    try {
      const value = await api<ObservationSummary>(`${path}/summary`);
      if (mounted.current && summaryEpoch.current.isCurrent(token)) setSummary(value);
    } catch (e) { if (mounted.current && summaryEpoch.current.isCurrent(token)) setSummaryError(describeError(e)); }
    finally { if (mounted.current && summaryEpoch.current.isCurrent(token)) setSummaryLoading(false); }
  }
  const blocked = !draftReady || draftBlocked || !mutation.ready || mutation.blocked || !!mutation.pending || !!busy;
  return <>
    <GuidedObservation workspaceId={workspaceId} researchId={researchId} revisionId={revisionId} visible={visible}
      onOpen={onOpen} onRecordingChange={onRecordingChange} blocked={blocked} submittedGuided={submittedGuided}
      onReady={prepareGuided} onInvalidate={invalidateGuided} />
    <div hidden={!visible}>
    <section className="panel" aria-label="流程观测导入">
      <h2>记录保存与导入</h2>
      <details><summary>高级：JSON 导入与原始模板</summary>
      <p>已有外部记录或自动化验收样例可在此导入。日常记录使用上方引导表单。</p>
      <p className="fine-print">当前研究 {researchId}；当前修订 {revisionId || "未选择"}。JSON 中的修订、运行和审核由服务核对，不会自动替换成当前修订。来源仅是声明，不是身份认证。</p>
      <div className="button-row"><button className="button" disabled={!!busy} onClick={() => void template()}>下载空白观测模板</button><button className="button small" disabled={!draft.raw} onClick={() => downloadJson({raw:draft.raw, declared_source:draft.source}, "workflow-observation-draft-backup.json")}>下载原草稿备份</button></div>
      <p className="fine-print">模板需要在实际使用后填写；预检和导入不会生成观测或通过研究审核。下载的原草稿备份包含原始文本与声明，不是可直接导入的观测。</p>
      <fieldset disabled={blocked}>
        <Field label="上传观测 JSON" hint="单条观测，最大 256 KiB；上传后需重新声明来源。"><input type="file" accept=".json,application/json" onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = ""; }} /></Field>
        <Field label="观测 JSON" hint="草稿仅保存在当前浏览器、当前工作区与研究；未点击确认导入前不写入服务。"><textarea value={draft.raw} onChange={(e) => edit({...draft,raw:e.target.value})} rows={16} spellCheck={false} /></Field>
        <Field label="观测声明来源" hint="必须与 JSON 中 source 一致；自动化样例不会计入人工完成率或效率对照。"><select value={draft.source} onChange={(e) => edit({...draft,source:e.target.value as ObservationSource | ""})}><option value="">请明确选择来源</option>{Object.entries(observationSources).map(([id,label]) => <option key={id} value={id}>{label}</option>)}</select></Field>
        <button className="button" disabled={!draft.raw.trim() || !draft.source} onClick={() => void validate()}>预检观测记录</button>
      </fieldset>
      </details>
      {notice && <p role="status">{notice}</p>}
      {draftError && <p role="alert">{draftError}</p>}
      {error && <p role="alert">{error}</p>}
      {mutation.error && <p role="alert">{mutation.error}</p>}
      {mutation.pending && <section className="info-note" aria-label="观测提交恢复">
        <p>{mutation.pending.state === "rejected" ? "原观测请求已明确拒绝，解除后可修改草稿。" : "观测提交结果待确认，服务器可能已保存。使用原请求安全重试，不会生成新的请求标识。"}</p>
        <JsonDetails value={mutation.pending.body} label="查看待确认原观测请求" />
        {mutation.pending.state === "rejected" ? <button className="button" disabled={!!busy} onClick={mutation.dismissRejected}>解除被拒绝的观测请求</button> : <button className="button" disabled={!!busy} onClick={() => void save(true)}>安全重试观测导入</button>}
      </section>}
      <fieldset disabled={blocked}>
        {validation && <section aria-label="观测预检结果">
          <h3>预检通过，尚未保存</h3><p>已核对记录格式、时间区间和结果关联。请确认记录来自所声明来源。</p>
          <p>声明来源：{validation.draft.source==="human"?"本人实际观测":"自动化验收样例"}。{validation.value.binding.kind==="external_declared"?"CLI 输出为外部声明。":validation.value.binding.outputs_verified?"已绑定核验通过的工作台结果。":"尚未绑定核验通过的工作台结果。"}</p>
          <Timing value={validation.value.timing} />
          <details><summary>完整核验说明与限制</summary>
            <JsonDetails value={{observation_digest:validation.value.observation_digest,binding:validation.value.binding}} label="查看协议、输入、结果绑定与核验限制" />
            <ul>{validation.value.warnings.map((warning,index) => <li key={index}>{warning}</li>)}</ul>
          </details>
          <Field label="确认来源与不可变保存"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /></Field>
          <p className="fine-print">我确认内容来自所声明来源，并了解保存后只能另建记录补充，不能改写原记录。此确认不等于研究结论通过。</p>
          <button className="button primary" disabled={!confirmed} onClick={() => void save()}>确认导入不可变观测</button>
        </section>}
      </fieldset>
    </section>
    <section className="panel" aria-label="已保存流程观测">
      <h2>已保存观测</h2>
      <Field label="观测列表每页数量"><select value={pageSize} onChange={(e) => {const size = Number(e.target.value); pageSizeRef.current = size; setPageSize(size); setList([]); setTotal(0); setNextOffset(0); void loadList(0).catch(() => undefined);}}>{[1,5,20].map((size) => <option key={size} value={size}>{size} 项</option>)}</select></Field>
      <div className="button-row"><button className="button small" disabled={listLoading} onClick={() => void loadList(0).catch(() => undefined)}>刷新观测列表</button>{nextOffset < total && <button className="button small" disabled={listLoading} onClick={() => void loadList(nextOffset).catch(() => undefined)}>加载更多观测</button>}</div>
      <p>已加载 {list.length} 项，总计 {total} 项。</p>{listError && <p role="alert">{listError}</p>}
      <div className="table-scroll"><table><thead><tr><th>记录 / 来源</th><th>路径 / 完成情况</th><th>保存时间 / 摘要</th><th>操作</th></tr></thead><tbody>{list.map((row) => <tr key={row.id}>
        <td>{row.id}<br />{observationSources[row.observation.source]} · {row.observation.practice_session ? "练习" : "非练习"}</td>
        <td>{conditionName(row.observation.condition)}<br />{completionName(row.observation.completion)}</td><td>{formatDate(row.created_at)}<br /><code>{row.payload_digest}</code></td>
        <td><button className="button small" disabled={detailLoading === row.id} onClick={() => void showDetail(row.id)}>查看观测 {shortId(row.id)}</button><DownloadRecord path={path} id={row.id} /></td>
      </tr>)}</tbody></table></div>
      {detailError && <p role="alert">{detailError}</p>}
      {detail && <article aria-label="不可变观测详情"><h3>观测 {detail.id}</h3>
        <p>{observationSources[detail.observation.source]} · {detail.observation.participant} · {conditionName(detail.observation.condition)} · {completionName(detail.observation.completion)}。内容完整性已校验；不代表人工身份或研究结论得到认证。</p>
        <p>内容摘要：<code>{detail.payload_digest}</code></p><DownloadRecord path={path} id={detail.id} /><Timing value={detail.timing} />
        <JsonDetails value={detail} label="查看不可变观测、绑定依据及全部限制" />
      </article>}
    </section>
    <section className="panel" aria-label="流程观测汇总">
      <div className="panel-heading"><h2>人工记录与个案对照</h2><button className="button" disabled={summaryLoading || busy === "save"} onClick={() => void loadSummary()}>计算观测汇总</button></div>
      <p>只展示服务端对已保存记录计算的结果。未满足同协议、参与者、输入、版本与环境条件时列出原因；不从自动化验收或局部审核时间推导效率提升。</p>
      {summaryError && <p role="alert">{summaryError}</p>}
      {summary && <Summary value={summary} />}
    </section>
    </div>
  </>;
}

function conditionName(value: string) { return value === "manual_cli" ? "手动 CLI" : "工作台"; }
function completionName(value: string) { return ({completed:"已完成",incomplete:"未完成",abandoned:"已放弃"} as Record<string,string>)[value] || value; }
function DownloadRecord({path,id}: {path:string;id:string}) {
  return <a className="button small" href={`/api${path}/${encodeURIComponent(id)}/export`} download aria-label={`导出观测 JSON ${id}`}>导出 JSON</a>;
}
function Timing({value}: {value:ObservationTiming}) {
  return <section aria-label="服务计算观测耗时">
    <p>已观测的主动工作合计：{observedSeconds(value.observed_active_seconds)}；等待：{observedSeconds(value.observed_waiting_seconds)}；离开：{observedSeconds(value.observed_away_seconds)}。</p>
    <p>六阶段完整主动耗时：{observedSeconds(value.full_active_seconds)}。中断区间 {value.interrupted_intervals} 个；局部合计不代替全流程耗时。</p>
    <div className="table-scroll"><table><thead><tr><th>阶段</th><th>主动</th><th>等待</th><th>离开</th><th>结束 / 中断区间</th></tr></thead><tbody>{value.phases.map((phase) => <tr key={phase.phase}><th>{observationPhases[phase.phase] || phase.phase}</th><td>{observedSeconds(phase.active_seconds)}</td><td>{observedSeconds(phase.waiting_seconds)}</td><td>{observedSeconds(phase.away_seconds)}</td><td>{phase.ended_intervals} / {phase.interrupted_intervals}</td></tr>)}</tbody></table></div>
  </section>;
}
function Summary({value}: {value:ObservationSummary}) {
  return <div aria-label="服务计算观测汇总">
    <p>服务端生成于 {formatDate(value.generated_at)}，共 {value.total} 条记录。</p>
    <p>非练习人工声明记录：{value.human_nonpractice_records}；已完成：{value.human_completed_records}；完成率：{value.human_completion_rate === null ? "无适用样本" : `${(value.human_completion_rate * 100).toFixed(1)}%`}。</p>
    <p>自动化记录：{value.automation_records}；练习记录：{value.practice_records}。自动化与练习不进入人工完成率分母。</p>
    <div className="table-scroll"><table aria-label="各路径人工观测分母"><thead><tr><th>路径</th><th>非练习人工记录</th><th>完成 / 未完成 / 放弃</th><th>完成率</th><th>完整 / 部分耗时覆盖</th><th>含中断记录</th><th>错误与返工条目</th></tr></thead><tbody>{value.by_condition.map((condition) => <tr key={condition.condition}>
      <th>{conditionName(condition.condition)}</th><td>{condition.records}</td><td>{condition.completed} / {condition.incomplete} / {condition.abandoned}</td>
      <td>{condition.completion_rate === null ? "无适用样本" : `${(condition.completion_rate * 100).toFixed(1)}%`}</td><td>{condition.full_active_coverage_records} / {condition.partial_or_unmeasured_records}</td><td>{condition.interrupted_records}</td><td>{condition.error_and_rework_entries}</td>
    </tr>)}</tbody></table></div>
    <p className="fine-print">条目数按原始错误与返工声明统计，不代表独立错误数；完整耗时覆盖也不等于任务完成。</p>
    {!value.comparisons.length && <p>暂无满足分组条件的个案对照。</p>}
    {value.comparisons.map((comparison) => <article key={comparison.group_digest} aria-label={`观测对照 ${comparison.group_digest}`}>
      <p>{comparison.comparable ? "可列同声明条件个案差值" : "不能生成耗时差值"}：工作台减手动 CLI，{comparison.comparable ? observedSeconds(comparison.active_seconds_delta) : "—"}。</p>
      <ul>{comparison.reasons.map((reason,index) => <li key={index}>{reason}</li>)}</ul>
      <JsonDetails value={comparison} label="查看对照记录及可比性依据" />
    </article>)}
    <ul>{value.limitations.map((limitation,index) => <li key={index}>{limitation}</li>)}</ul>
    <button className="button small" onClick={() => downloadJson(value, `workflow-observation-summary-${value.research_id}.json`)}>下载当前服务汇总 JSON</button>
    <p className="fine-print">下载保留当前服务响应的生成时间、原始记录和分母；新增记录后请重新计算。个案差值不能推断普遍效率提升。</p>
    <JsonDetails value={value} label="查看服务汇总、全部记录及限制" />
  </div>;
}
