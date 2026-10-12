import {useCallback, useEffect, useRef, useState} from "react";
import {api} from "../api";
import {parseStudyScan, studyDecisionNames, studyFileError, studyPanelIds, studyReviewBody, studyStatusName} from "../authorStudies";
import {describeError, formatDate} from "../domain";
import type {StudyDetail, StudyPage, StudyReview, StudyReviews} from "../generated/api-contract";
import {recoveryKey} from "../reviewRecovery";
import {useRecoverableMutation} from "../useRecoverableMutation";
import type {WorkspaceRequestScope} from "../workspaceScope";
import MutationRecovery from "./MutationRecovery";
import {Field, JsonDetails} from "./ui";
import ResearchBindings from "./ResearchBindings";

const base = "/author-studies";
type RevisionParent = {review: StudyReview; study: StudyDetail};
export default function AuthorStudies({workspaceScope, initialId = ""}: {workspaceScope: WorkspaceRequestScope; initialId?: string}) {
  const [title, setTitle] = useState(""), [note, setNote] = useState(""), [panelText, setPanelText] = useState("");
  const [scan, setScan] = useState<Record<string, unknown> | null>(null), [fileName, setFileName] = useState("");
  const [parent, setParent] = useState<RevisionParent | null>(null);
  const [reading, setReading] = useState(false), [sending, setSending] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [page, setPage] = useState<StudyPage | null>(null), [offset, setOffset] = useState(0), [listError, setListError] = useState("");
  const [refresh, setRefresh] = useState(0), [selected, setSelected] = useState(initialId);
  const mounted = useRef(false), readEpoch = useRef(0), busy = useRef(false), input = useRef<HTMLInputElement>(null);
  const mutation = useRecoverableMutation<StudyDetail>(recoveryKey(workspaceScope.workspaceId, "author-study-create-request"), base);
  const changed = useCallback(() => setRefresh(value => value + 1), []);
  useEffect(() => { mounted.current = true; return () => {mounted.current = false; readEpoch.current++;}; }, []);
  useEffect(() => {
    const workspace = workspaceScope.capture(), abort = new AbortController();
    setPage(null); setListError("");
    void api<StudyPage>(`${base}?limit=20&offset=${offset}`, {signal: abort.signal}).then(value => {
      if (!workspaceScope.isCurrent(workspace, abort.signal)) return;
      setPage(value); setSelected(previous => previous || value.items[0]?.id || "");
    }).catch(e => {if (workspaceScope.isCurrent(workspace, abort.signal)) setListError(describeError(e));});
    return () => abort.abort();
  }, [workspaceScope, offset, refresh]);
  async function chooseFile(file: File | null) {
    const epoch = ++readEpoch.current, workspace = workspaceScope.capture();
    setScan(null); setFileName(""); setError(""); setReading(false);
    const invalid = studyFileError(file); if (invalid || !file) {setError(invalid); return;}
    setReading(true);
    try {
      const parsed = parseStudyScan(await file.text());
      if (mounted.current && epoch === readEpoch.current && workspaceScope.isCurrent(workspace)) {setScan(parsed); setFileName(file.name);}
    } catch (e) {if (mounted.current && epoch === readEpoch.current && workspaceScope.isCurrent(workspace)) setError(describeError(e));}
    finally {if (mounted.current && epoch === readEpoch.current && workspaceScope.isCurrent(workspace)) setReading(false);}
  }
  function clearFile() {readEpoch.current++; setScan(null); setFileName(""); setReading(false); if (input.current) input.current.value = "";}
  const locked = sending || !!mutation.pending || mutation.blocked || !mutation.ready;
  function revise(value: RevisionParent) {
    if (locked || busy.current) return;
    clearFile(); setParent(value); setTitle(`${value.study.title.slice(0, 190)} · 修订`); setNote("");
    setPanelText(value.study.author_panel_ids.join("\n")); setError("");
    setNotice(`修订入口已关联审核 ${value.review.id}。请通过 CLI 修改计划并重新扫描，再选择新的 input.json；新版本需要自己的审核。`);
  }
  async function submit(body?: Record<string, unknown>) {
    if (busy.current) return; busy.current = true;
    const workspace = workspaceScope.capture(); setSending(true); setError(""); setNotice("");
    try {
      const saved = await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(workspace)) return;
      setSelected(saved.id); setOffset(0); changed();
      try { mutation.acknowledge(() => {clearFile(); setTitle(""); setNote(""); setPanelText(""); setParent(null);}); }
      catch (e) {setNotice(`准入记录已保存：${saved.id}；本机确认未完成，请用原请求安全重试。${describeError(e)}`); return;}
      setNotice(`准入记录已保存：${saved.id}。新版本不会继承旧审核。`);
    } catch (e) {if (mounted.current && workspaceScope.isCurrent(workspace)) setError(describeError(e));}
    finally {busy.current = false; if (mounted.current && workspaceScope.isCurrent(workspace)) setSending(false);}
  }
  function create() {
    try {
      const ids = studyPanelIds(panelText);
      if (scan && title.trim()) void submit({title: title.trim(), note, scan, parent_review_id: parent?.review.id ?? null, author_panel_ids: ids});
    } catch (e) {setError(describeError(e));}
  }
  return <>
    <section className="panel" aria-label="研究准入导入">
      <h2>导入扫描 → 检查资格与依据 → 审核 → 必要时修订</h2>
      <p className="info-note">作者公开数据经过扰动及随机删除。本入口检查全原行聚合扫描能否达到预先声明的数量门槛；不计算组合收益，不自动放宽窗口或删除未达标月份。</p>
      <p>服务器核验范围仅为聚合输入一致性（aggregate_consistency_only），未读取原 MAT 重算计数。完整源复算应通过 CLI 完成。数量达标仍不表示组合方法已经实现。</p>
      {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      <MutationRecovery name="研究准入" pending={mutation.pending} error={mutation.error} busy={sending} onRetry={() => void submit()} onDismiss={mutation.dismissRejected}/>
      <fieldset disabled={locked}>
        <Field label="研究准入标题"><input maxLength={200} value={title} onChange={e => setTitle(e.target.value)}/></Field>
        <Field label="研究准入说明"><textarea maxLength={4000} value={note} onChange={e => setNote(e.target.value)}/></Field>
        <Field label="研究准入扫描 input.json"><input ref={input} type="file" accept=".json,application/json" onChange={e => void chooseFile(e.target.files?.[0] || null)}/></Field>
        <p className="fine-print">使用 CLI 生成的扫描 input.json，最多 256 KiB、180 个连续开发月份。原文件 MAT 留在 CLI 工作目录。</p>
        {reading && <p role="status">正在读取扫描文件。</p>}{scan && <p role="status">已读取 {fileName}，提交后核验完整计划及聚合计数。</p>}
        <details><summary>可选：关联逐行作者面板</summary><Field label="关联作者面板 ID"><textarea value={panelText} onChange={e => setPanelText(e.target.value)} maxLength={1000}/></Field><p>最多 8 个完整 ID，每行一个。服务会核验面板同源、目标月在开发区间内；面板不能代替全原行扫描。</p></details>
        {parent ? <section className="info-note" aria-label="修订来源"><p>旧研究：{parent.study.title} · {parent.study.id}</p><p>触发审核：{parent.review.id} · {studyDecisionNames[parent.review.decision]} · {parent.review.actor}</p><p>旧摘要：{parent.review.study_digest}。请导入不同扫描；服务保存变化，新版本不继承旧审核。</p><button className="button small" onClick={() => {setParent(null); clearFile();}}>取消修订关联，改为独立研究</button></section> : <p>当前为独立研究。修订已有研究时，在下方已保存审核中选择“以此审核创建修订”。</p>}
        <button className="button primary" disabled={!scan || !title.trim() || reading} onClick={create}>导入并检查研究准入</button>
      </fieldset>
    </section>
    <section className="panel" aria-label="研究准入历史"><h2>研究准入历史</h2>
      {listError && <p role="alert">目录读取失败，已保存记录保持不变。{listError}</p>}
      <div className="button-row"><button className="button small" onClick={changed}>刷新研究准入历史</button><button className="button small" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 20))}>上一页准入研究</button><button className="button small" disabled={!page || offset + page.items.length >= page.total} onClick={() => setOffset(value => value + 20)}>下一页准入研究</button></div>
      <p>{page ? `本页 ${page.items.length} / 共 ${page.total} 项；从第 ${offset + 1} 项开始。` : "正在读取准入研究目录。"}</p>
      {page?.total === 0 && <p>尚无研究准入记录。先冻结计划并通过 CLI 运行源扫描。</p>}
      <div className="table-scroll"><table><thead><tr><th>研究</th><th>数据与开发区间</th><th>任务 / 门槛</th><th>筛查结果</th></tr></thead><tbody>{page?.items.map(item => <tr key={item.id}>
        <td><button className="text-button" aria-label={`查看准入研究 ${item.id}`} onClick={() => setSelected(item.id)}>{item.title}</button></td><td>{item.plan.source_file}<br/>{item.plan.development_start} → {item.plan.development_end}</td><td>{item.plan.task} {item.plan.requires_market_cap ? "+ MV" : ""} / {item.plan.minimum_assets}</td><td>{studyStatusName(item.summary.status)}</td>
      </tr>)}</tbody></table></div>
    </section>
    {selected && <AuthorStudyDetail key={`${workspaceScope.key}:${selected}`} id={selected} workspaceScope={workspaceScope} onRevise={revise} revisionLocked={locked}/>}
  </>;
}

function AuthorStudyDetail({id, workspaceScope, onRevise, revisionLocked}: {id: string; workspaceScope: WorkspaceRequestScope; onRevise: (parent: RevisionParent) => void; revisionLocked: boolean}) {
  const [detail, setDetail] = useState<StudyDetail | null>(null), [reviews, setReviews] = useState<StudyReview[] | null>(null);
  const [error, setError] = useState(""), [loading, setLoading] = useState(false), [refresh, setRefresh] = useState(0), [monthOffset, setMonthOffset] = useState(0);
  const changed = useCallback(() => setRefresh(value => value + 1), []);
  useEffect(() => {
    const workspace = workspaceScope.capture(), abort = new AbortController();
    setDetail(null); setReviews(null); setError(""); setLoading(true);
    void Promise.all([api<StudyDetail>(`${base}/${encodeURIComponent(id)}`, {signal: abort.signal}), api<StudyReviews>(`${base}/${encodeURIComponent(id)}/reviews`, {signal: abort.signal})]).then(([record, page]) => {
      if (workspaceScope.isCurrent(workspace, abort.signal)) {setDetail(record); setReviews(page.items);}
    }).catch(e => {if (workspaceScope.isCurrent(workspace, abort.signal)) setError(`完整核验未通过；旧结果与旧审核已隐藏。${describeError(e)}`);})
      .finally(() => {if (workspaceScope.isCurrent(workspace, abort.signal)) setLoading(false);});
    return () => abort.abort();
  }, [id, workspaceScope, refresh]);
  return <section className="panel" aria-label="研究准入详情" data-study-id={id}>
    <h2>研究准入详情</h2><button className="button" disabled={loading} onClick={changed}>完整核验并刷新准入结果</button>
    {loading && <p role="status">正在读取并核验准入研究与审核绑定。</p>}{error && <p role="alert">{error}</p>}
    {detail && <>
      <h3>{detail.title}</h3><p>{detail.note}</p><p>{formatDate(detail.created_at)} · 研究 {detail.id} · 摘要 {detail.digest}</p>
      <ResearchBindings key={`${workspaceScope.key}:${detail.id}:${detail.digest}`} study={detail} workspaceScope={workspaceScope}/>
      <section aria-label="研究准入输出"><h3>{studyStatusName(detail.result.summary.status)}</h3>
        <p>达标 {detail.result.summary.months_meeting_threshold} / {detail.result.summary.months} 个月；所选任务可用资产最少 {detail.result.summary.min_selected}、最多 {detail.result.summary.max_selected}。只有每个月达到冻结门槛才视为筛查达标。</p>
        <p className="info-note">组合执行就绪：否。当前只完成数据数量筛查；原文过滤、分组、极值处理、中性化、权重与标签缺失处理尚未在这条路径实现。</p>
        <p>核验范围：{detail.verification_scope}；原 MAT 在服务器重新核验：{detail.raw_source_reverified ? "是" : "否"}。浏览器展示服务端计算结果，不将导入聚合计数称为源数据复算。</p>
        <h4>冻结计划</h4>
        <p>来源 {detail.result.plan.source_file}；任务 {detail.result.plan.task === "momentum_dgw" ? "MOM + DGW" : "MOM"}{detail.result.plan.requires_market_cap ? " + MV" : "（不要求 MV）"}；最低资产数 {detail.result.plan.minimum_assets}。</p>
        <p>门槛依据：{detail.result.plan.threshold_origin === "table8_initial_upper_bound" ? "Table8 初始 9 组 × 50，仅为必要数量条件上界筛查" : "项目筛查阈值，不是论文规则或统计充分性证明"}。{detail.result.plan.rationale}</p>
        <p>连续开发区间：{detail.result.plan.development_start} 至 {detail.result.plan.development_end}；保留区间起点：{detail.result.plan.reserved_from}。此边界为计划声明，不证明数据从未被观察。</p>
        <p>MOM：H−12 至 H−2 共 11 个完整自然月；DGW / MV：H−1。H 月标签不参与资格扫描，不通过未来标签筛选形成样本。</p>
        <h4>逐月四种可用性</h4>
        <p>当前显示第 {monthOffset + 1}–{Math.min(monthOffset + 20, detail.result.months.length)} 月，共 {detail.result.months.length} 月。缺历史、非法和数值不可表示是互斥原因计数。</p>
        <div className="button-row"><button className="button small" disabled={!monthOffset} onClick={() => setMonthOffset(value => Math.max(0, value - 20))}>上一页准入月份</button><button className="button small" disabled={monthOffset + 20 >= detail.result.months.length} onClick={() => setMonthOffset(value => value + 20)}>下一页准入月份</button></div>
        <div className="table-scroll"><table><thead><tr><th>月 / 原行</th><th>MOM</th><th>MOM + DGW</th><th>MOM + MV</th><th>MOM + DGW + MV</th><th>所选 / 门槛</th><th>历史缺失 / 非法 / 不可表示</th></tr></thead><tbody>{detail.result.months.slice(monthOffset, monthOffset + 20).map(row => <tr key={row.month}>
          <th>{row.month} / {row.assets}</th><td>{row.momentum_ready}</td><td>{row.momentum_dgw_ready}</td><td>{row.momentum_mv_ready}</td><td>{row.momentum_dgw_mv_ready}</td><td>{row.selected_ready} / {row.threshold_met ? "达标" : "未达标"}</td><td>{row.momentum_missing} / {row.momentum_invalid} / {row.momentum_unrepresentable}</td>
        </tr>)}</tbody></table></div>
        <h4>论文及规则依据</h4><p>论文 DOI {detail.result.evidence.doi} · PDF SHA256 {detail.result.evidence.pdf_sha256}</p>
        <ul>{detail.result.evidence.citations.map(citation => <li key={citation.id}><strong>{citation.origin === "paper" ? "论文" : citation.origin === "author_code" ? "作者代码" : "项目约定"}</strong> · {citation.locator}：{citation.claim}</li>)}</ul>
        <ul>{detail.result.limitations.map((value, i) => <li key={i}>{value}</li>)}</ul>
        <a className="button" href={`/api${base}/${encodeURIComponent(id)}/markdown`} download={`author-study-${id}.md`}>下载研究准入报告</a>
        <JsonDetails label="输入、计划和源摘要及关联面板" value={{input_digest: detail.result.input_digest, plan_digest: detail.result.plan_digest, source: detail.result.source, author_panel_ids: detail.author_panel_ids}}/>
      </section>
      <section aria-label="研究准入修订关系"><h3>修订关系</h3><p>父研究：{detail.parent_study_id ?? "无"}；触发审核：{detail.parent_review_id ?? "无"}。本版本的审核只来自下方本版本记录。</p>
        {!!detail.changes.length && <div className="table-scroll"><table><thead><tr><th>字段</th><th>旧值</th><th>新值</th></tr></thead><tbody>{detail.changes.map(change => <tr key={change.field}><th>{change.field}</th><td>{change.before}</td><td>{change.after}</td></tr>)}</tbody></table></div>}
      </section>
    </>}
    <StudyReviewActions id={id} detail={detail} workspaceScope={workspaceScope} onSaved={changed}/>
    {detail && reviews && <section aria-label="研究准入审核历史"><h3>本版本审核</h3>
      {!reviews.length && <p>本版本尚无审核，不继承旧版本审核结论。</p>}
      {reviews.map(review => <article key={review.id}><h4>{studyDecisionNames[review.decision]} · {review.actor === "human" ? "声明为人工" : "自动化"}</h4><p>{review.note}</p><p>{review.id} · {formatDate(review.created_at)} · 绑定研究 {review.study_id} · 摘要 {review.study_digest}</p>
        <button className="button small" aria-label={`以审核 ${review.id} 创建修订`} disabled={revisionLocked} onClick={() => onRevise({review, study: detail})}>以此审核创建修订</button>
      </article>)}
    </section>}
  </section>;
}

function StudyReviewActions({id, detail, workspaceScope, onSaved}: {id: string; detail: StudyDetail | null; workspaceScope: WorkspaceRequestScope; onSaved: () => void}) {
  const [decision, setDecision] = useState<StudyReview["decision"]>("rules_unresolved"), [note, setNote] = useState("");
  const [actor, setActor] = useState<StudyReview["actor"]>("human"), [sending, setSending] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const mounted = useRef(false), busy = useRef(false);
  const mutation = useRecoverableMutation<StudyReview>(recoveryKey(workspaceScope.workspaceId, "author-study-review-request", id), `${base}/${encodeURIComponent(id)}/reviews`);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  async function submit(body?: Record<string, unknown>) {
    if (busy.current) return; busy.current = true;
    const workspace = workspaceScope.capture(); setSending(true); setError(""); setNotice("");
    try {
      const saved = await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(workspace)) return;
      try {mutation.acknowledge(() => setNote(""));}
      catch (e) {setNotice(`审核已保存：${saved.id}；本机确认未完成。${describeError(e)}`); return;}
      setNotice(`审核已保存：${saved.id}，绑定原研究摘要 ${saved.study_digest}。`); onSaved();
    } catch (e) {if (mounted.current && workspaceScope.isCurrent(workspace)) setError(describeError(e));}
    finally {busy.current = false; if (mounted.current && workspaceScope.isCurrent(workspace)) setSending(false);}
  }
  function save() {try {void submit(studyReviewBody(detail, decision, note, actor));} catch (e) {setError(describeError(e));}}
  const locked = !detail || sending || !!mutation.pending || mutation.blocked || !mutation.ready;
  return <section aria-label="研究准入审核"><h3>记录本次审核</h3>
    <p>审核绑定当前可见研究及其摘要。“接受”仅接受筛查结果及限制，不代表批准组合运行或证明策略有效。声明来源是本地用户选择，不是认证身份。</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <MutationRecovery name="准入审核" pending={mutation.pending} error={mutation.error} busy={sending} onRetry={() => void submit()} onDismiss={mutation.dismissRejected}/>
    <fieldset disabled={locked}>
      <Field label="准入审核结论"><select value={decision} onChange={e => setDecision(e.target.value as StudyReview["decision"])}>{Object.entries(studyDecisionNames).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
      <Field label="准入审核声明来源"><select value={actor} onChange={e => setActor(e.target.value as StudyReview["actor"])}><option value="human">人工审核（当前用户声明）</option><option value="automation">自动化验收</option></select></Field>
      <Field label="准入审核依据"><textarea maxLength={4000} value={note} onChange={e => setNote(e.target.value)}/></Field>
      <button className="button primary" disabled={!note.trim()} onClick={save}>保存准入审核</button>
    </fieldset>
  </section>;
}
