import {useEffect, useRef, useState} from "react";
import {api} from "../api";
import {describeError, formatDate} from "../domain";
import type {CaseContext, CaseDetail, CasePage, CasePreview, MonthlyPage, StudyPage, IndustryMomExperimentPage} from "../generated/api-contract";
import {caseActionNames, caseCreateBody, caseOriginNames, caseSourceNames, caseStateNames, caseSourceCollection, resultHighlights} from "../researchCases";
import type {ResearchCaseSource} from "../researchCases";
import {recoveryKey} from "../reviewRecovery";
import {useRecoverableMutation} from "../useRecoverableMutation";
import type {WorkspaceRequestScope} from "../workspaceScope";
import MutationRecovery from "./MutationRecovery";
import ResearchToolSession from "./ResearchToolSession";
import ResearchClaims from "./ResearchClaims";
import {Field, JsonDetails} from "./ui";

type SourceKind = ResearchCaseSource;
type SourceOption = {id:string; label:string};
export default function ResearchCases({workspaceScope, onOpenSource, initialId = ""}: {
  workspaceScope: WorkspaceRequestScope; onOpenSource: (kind:SourceKind, id:string) => void; initialId?: string;
}) {
  const [kind, setKind] = useState<SourceKind>("author_study"), [sourceId, setSourceId] = useState("");
  const [sources, setSources] = useState<SourceOption[]>([]), [sourceError, setSourceError] = useState("");
  const [cursors, setCursors] = useState([0]), [nextCursor, setNextCursor] = useState<number|null>(null);
  const [preview, setPreview] = useState<CasePreview|null>(null), [previewError, setPreviewError] = useState("");
  const [title, setTitle] = useState(""), [note, setNote] = useState("");
  const [page, setPage] = useState<CasePage|null>(null), [offset, setOffset] = useState(0), [selected, setSelected] = useState(initialId);
  const [refresh, setRefresh] = useState(0), [error, setError] = useState(""), [sending, setSending] = useState(false);
  const mounted = useRef(false), busy = useRef(false);
  const mutation = useRecoverableMutation<CaseDetail>(recoveryKey(workspaceScope.workspaceId, "research-case-create"), "/research-cases");
  const cursor = cursors[cursors.length - 1];
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController();
    setSources([]); setSourceError(""); setNextCursor(null); setSourceId(""); setPreview(null);
    async function load() {
      let items:SourceOption[], next:number|null;
      if (kind === "daily_run") {
        const result = await api<{items:{id:string; status:string; created_at:string; mode:string}[]; has_more:boolean; next_cursor:number}>(`/catalog/runs?limit=20&after=${cursor}`, {signal:abort.signal});
        items = result.items.filter(row => row.status === "completed").map(row => ({id:row.id, label:`${formatDate(row.created_at)} · ${row.mode} · ${row.id}`}));
        next = result.has_more ? result.next_cursor : null;
      } else {
        const path = caseSourceCollection(kind);
        const result = await api<StudyPage|MonthlyPage|IndustryMomExperimentPage>(`${path}?limit=20&offset=${cursor}`, {signal:abort.signal});
        items = result.items.filter(row => !("status" in row) || row.status === "completed").map(row => ({id:row.id, label:"title" in row ? row.title : `${formatDate(row.created_at)} · ${row.id}`}));
        next = cursor + 20 < result.total ? cursor + 20 : null;
      }
      if (workspaceScope.isCurrent(captured, abort.signal)) {setSources(items); setNextCursor(next);}
    }
    void load().catch(e => {if (workspaceScope.isCurrent(captured, abort.signal)) setSourceError(describeError(e));});
    return () => abort.abort();
  }, [kind, cursor, refresh, workspaceScope]);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController();
    setPreview(null); setPreviewError("");
    if (sourceId) void api<CasePreview>(`/research-cases/preview?${new URLSearchParams({source_kind:kind, source_id:sourceId})}`, {signal:abort.signal}).then(value => {
      if (workspaceScope.isCurrent(captured, abort.signal)) {setPreview(value); setTitle(previous => previous || `${caseSourceNames[kind]} · ${sourceId.slice(-12)}`);}
    }).catch(e => {if (workspaceScope.isCurrent(captured, abort.signal)) setPreviewError(describeError(e));});
    return () => abort.abort();
  }, [kind, sourceId, workspaceScope]);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController();
    setPage(null); setError("");
    void api<CasePage>(`/research-cases?limit=20&offset=${offset}`, {signal:abort.signal}).then(value => {
      if (workspaceScope.isCurrent(captured, abort.signal)) {setPage(value); setSelected(previous => previous || value.items[0]?.id || "");}
    }).catch(e => {if (workspaceScope.isCurrent(captured, abort.signal)) setError(describeError(e));});
    return () => abort.abort();
  }, [offset, refresh, workspaceScope]);
  async function submit(body?:Record<string,unknown>) {
    if (busy.current) return; busy.current=true; setSending(true); setError("");
    const captured = workspaceScope.capture();
    try {
      const saved = await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
      setSelected(saved.id); setOffset(0); setRefresh(v => v+1);
      mutation.acknowledge(() => {setTitle(""); setNote("");});
    } catch(e) {if (mounted.current && workspaceScope.isCurrent(captured)) setError(describeError(e));}
    finally {busy.current=false; if (mounted.current && workspaceScope.isCurrent(captured)) setSending(false);}
  }
  function create() {try {void submit(caseCreateBody(preview,title,note));} catch(e) {setError(describeError(e));}}
  const locked = sending || !!mutation.pending || mutation.blocked || !mutation.ready;
  return <>
    <section className="panel" aria-label="研究任务入口">
      <h2>研究任务：依据 → 结果 → 下一步</h2>
      <p>将已经完成的计算或准入筛查关联为一项可检查的任务。工具只读取确定性结果、记录待审核建议；AI 接口尚未启用。</p>
      {error && <p role="alert">{error}</p>}
      <MutationRecovery name="研究任务" pending={mutation.pending} error={mutation.error} busy={sending} onRetry={() => void submit()} onDismiss={mutation.dismissRejected}/>
      <details><summary>从现有结果建立研究任务</summary>
        <fieldset disabled={locked}>
          <Field label="研究任务来源"><select value={kind} onChange={e => {setKind(e.target.value as SourceKind); setCursors([0]); setSourceId(""); setPreview(null);}}>{Object.entries(caseSourceNames).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
          {sourceError && <p role="alert">{sourceError}</p>}
          <Field label="选择已完成的研究结果"><select value={sourceId} onChange={e => {setSourceId(e.target.value); setPreview(null);}}><option value="">选择一项结果</option>{sources.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></Field>
          <div className="button-row"><button className="button small" disabled={cursors.length<=1} onClick={() => setCursors(v => v.slice(0,-1))}>上一页来源</button><button className="button small" disabled={nextCursor===null} onClick={() => {if(nextCursor!==null)setCursors(v => [...v,nextCursor]);}}>下一页来源</button></div>
          {!sources.length && <p>本页没有可关联的已完成结果。可翻页，或先在对应模块完成一次计算或扫描导入。</p>}
          {previewError && <p role="alert">核验未通过：{previewError}</p>}
          {sourceId && !preview && !previewError && <p role="status">正在核验具体结果与来源。</p>}
          {preview && <div className="info-note"><p>{caseStateNames[preview.context.state]}：{preview.context.stop_reason ?? "结果已生成，经济解释仍需人工评审。"}</p><p>{preview.context.data_scope}</p><p>{preview.context.method_scope}</p></div>}
          <Field label="研究任务名称"><input value={title} maxLength={200} onChange={e => setTitle(e.target.value)}/></Field>
          <Field label="研究任务备注"><textarea value={note} maxLength={4000} onChange={e => setNote(e.target.value)}/></Field>
          <button className="button primary" disabled={!preview||!title.trim()} onClick={create}>保存研究任务关联</button>
        </fieldset>
      </details>
    </section>
    <section className="panel" aria-label="研究任务目录"><h2>已保存任务</h2>
      <div className="button-row"><button className="button small" disabled={sending} onClick={() => setRefresh(v=>v+1)}>刷新研究任务</button><button className="button small" disabled={!offset} onClick={() => setOffset(v=>Math.max(0,v-20))}>上一页任务</button><button className="button small" disabled={!page||offset+20>=page.total} onClick={() => setOffset(v=>v+20)}>下一页任务</button></div>
      <p>{page ? `本页 ${page.items.length} / 共 ${page.total} 项。` : "正在读取任务目录。"}</p>
      {page?.items.map(item => <p key={item.id}><button className="text-button" aria-label={`查看研究任务 ${item.id}`} onClick={() => setSelected(item.id)}>{item.title}</button> · {caseSourceNames[item.source_kind]} · {caseStateNames[item.state]}</p>)}
    </section>
    {selected && <CaseDetailPanel key={`${workspaceScope.key}:${selected}`} identity={selected} refresh={refresh} workspaceScope={workspaceScope} onOpenSource={onOpenSource}/>}
  </>;
}

function CaseDetailPanel({identity, refresh, workspaceScope, onOpenSource}: {identity:string; refresh:number; workspaceScope:WorkspaceRequestScope; onOpenSource:(kind:SourceKind,id:string)=>void}) {
  const [detail,setDetail] = useState<CaseDetail|null>(null), [error,setError] = useState("");
  useEffect(() => {
    const captured=workspaceScope.capture(), abort=new AbortController(); setDetail(null); setError("");
    void api<CaseDetail>(`/research-cases/${identity}`,{signal:abort.signal}).then(value=>{if(workspaceScope.isCurrent(captured,abort.signal))setDetail(value);})
      .catch(e=>{if(workspaceScope.isCurrent(captured,abort.signal))setError(describeError(e));});
    return ()=>abort.abort();
  },[identity,refresh,workspaceScope]);
  if(error)return <section className="panel" role="alert">研究任务完整性核验未通过，旧输出已隐藏。{error}</section>;
  if(!detail)return <section className="panel" role="status">正在核验任务及其来源。</section>;
  return <section className="panel" aria-label="研究任务详情" data-case-id={identity}>
    <h2>{detail.title}</h2><p>{detail.note}</p><p>{caseSourceNames[detail.source_kind]} · {formatDate(detail.created_at)}</p>
    <div className="info-note"><h3>{caseStateNames[detail.context.state]}</h3><p>{detail.context.stop_reason ?? "工具计算已完成；解释与采纳由人工评审决定。"}</p><p>允许的下一步：{detail.context.allowed_actions.map(action=>caseActionNames[action]).join("；")}。</p></div>
    <ContextView context={detail.context}/>
    <div className="button-row"><button className="button" onClick={()=>onOpenSource(detail.source_kind,detail.source_id)}>打开对应结果与人工审核</button><a className="button" href={`/api/research-cases/${identity}/markdown`} download>下载研究任务记录</a></div>
    <JsonDetails label="任务身份与冻结来源" value={{id:detail.id,digest:detail.digest,source_id:detail.source_id,source_digest:detail.source_digest,provenance:detail.context.provenance}}/>
    <ResearchToolSession detail={detail} workspaceScope={workspaceScope}/>
    <ResearchClaims key={detail.id} detail={detail} workspaceScope={workspaceScope}/>
  </section>;
}

function ContextView({context}:{context:CaseContext}) {
  return <>
    <section aria-label="研究定义与证据"><h3>研究定义与依据</h3>
      {context.definitions.map(item=><article key={item.id}><strong>{caseOriginNames[item.attribution]}</strong><p>{item.text}</p><p className="fine-print">关联证据：{item.evidence_ids.join("、")||"无；不可视为已被论文支持"}</p></article>)}
      <details><summary>展开原文与规则依据（{context.evidence.length} 条）</summary>{context.evidence.map(item=><article key={item.id}><h4>{item.id} · {caseOriginNames[item.origin]}</h4><p>{item.text}</p><p>{item.locator} · {item.verification}</p></article>)}</details>
    </section>
    <section aria-label="研究实际输出"><h3>确定性工具输出</h3><p>{context.data_scope}</p><p>{context.method_scope}</p>
      {context.results.map(item=><article key={item.id}><h4>{item.summary}</h4><div className="table-scroll"><table><thead><tr><th>保存的指标 / 状态</th><th>实际值</th></tr></thead><tbody>{resultHighlights(item).map(row=><tr key={row.key}><td>{row.key}</td><td>{row.value}</td></tr>)}</tbody></table></div><JsonDetails label="查看完整计算输出" value={JSON.parse(item.payload_json)}/><p className="fine-print">结果摘要：{item.digest}</p></article>)}
      <ul>{context.limitations.map((value,i)=><li key={i}>{value}</li>)}</ul>
    </section>
    <details><summary>建立任务时的审核记录（{context.reviews.length} 条）</summary><p>这里只展示冻结时已有的审核。新审核在原结果页面查看；自动化提案不计为人工批准。</p>{context.reviews.map(review=><article key={review.id}><p>{review.decision} · {review.actor} · {review.scope}</p><p>{review.note}</p><p>语义审核状态：{review.assessment_summary?.semantic_status||"历史记录未保存结构化维度"}</p>{review.assessment&&<JsonDetails label="五个审核维度与实际计时" value={review.assessment}/>}</article>)}</details>
  </>;
}
