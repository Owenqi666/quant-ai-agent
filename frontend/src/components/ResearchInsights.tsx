import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { activeStatuses, describeError, formatDate, modeName, shortId, statusName } from "../domain";
import type { Research, Run } from "../domain";
import type { ComparisonCandidate, ComparisonRun, ReportList, ReportSummary, ResearchReport, RunComparison } from "../generated/api-contract";
import { addReportRun, InsightRequestEpoch, insightValue, metricDelta, reportRunLimit } from "../researchInsights";
import { recoveryKey } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { currentRunReport, revisionLabel } from "../dailyWorkflow";
import CatalogSelect from "./CatalogSelect";
import { Badge, Field, JsonDetails } from "./ui";

type Perform = (label: string, action: () => Promise<void>) => Promise<void>;
const describeRun = (run: Run) => `${statusName(run.status)} · ${modeName(run.mode)} · 研究 ${shortId(run.research_id)} · 修订 ${shortId(run.revision_id)}`;
const conditionNames: Record<string, string> = {
  paper: "论文版本", paper_sha256: "论文版本", dataset: "数据版本", dataset_sha256: "数据版本", data_metadata: "数据规则",
  evaluation: "评估配置", execution: "执行语义", recorded_semantics: "记录的执行语义", code_digest: "完整代码版本", numerical_code: "数值计算代码", environment: "执行环境",
  revision_digest: "研究修订摘要", budget: "预算", mode: "执行流程",
};
const conditionStatus = { equal: "一致", changed: "已变化", unknown: "未知" };

/** The parent must mount this component by workspace ID and research ID. */
export default function ResearchInsights({ workspaceId, research, selectedRun = null, busy, onPerform }: {
  workspaceId: string; research: Research; selectedRun?: Run | null; busy: string; onPerform: Perform;
}) {
  return <>
    <section className="panel">
      <h2>实验对比与冻结研究总结</h2>
      <p>为当前选中的实验生成可下载报告，保留论文依据、候选、结果与已保存审核。实验间对比是可选工具，不是生成报告的前提。</p>
      <p className="fine-print">当前研究：{research.title} · {research.id}。论文、候选和审核仍按原记录保留；实际人工耗时与研究优势需要另行评测。</p>
    </section>
    <ReportPanel workspaceId={workspaceId} research={research} selectedRun={selectedRun} busy={busy} onPerform={onPerform} />
    <details><summary>高级：比较两次实验</summary><ComparisonPanel /></details>
  </>;
}

function ComparisonPanel() {
  const [baseline, setBaseline] = useState("");
  const [candidate, setCandidate] = useState("");
  const [result, setResult] = useState<RunComparison | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const scope = useRef(new InsightRequestEpoch());
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => { scope.current.invalidate(); controller.current?.abort(); }, []);
  function select(side: "baseline" | "candidate", id: string) {
    scope.current.invalidate(); controller.current?.abort();
    setResult(null); setError(""); setLoading(false);
    (side === "baseline" ? setBaseline : setCandidate)(id);
  }
  async function compare() {
    controller.current?.abort();
    const abort = new AbortController(); controller.current = abort;
    const token = scope.current.begin();
    setLoading(true); setError(""); setResult(null);
    try {
      const query = new URLSearchParams({ baseline_run_id: baseline, candidate_run_id: candidate });
      const response = await api<RunComparison>(`/run-comparison?${query}`, { signal: abort.signal });
      if (scope.current.isCurrent(token) && !abort.signal.aborted) setResult(response);
    } catch (e) { if (scope.current.isCurrent(token) && !abort.signal.aborted) setError(describeError(e)); }
    finally { if (scope.current.isCurrent(token)) setLoading(false); }
  }
  return <section className="panel" aria-label="实验条件对比">
    <h2>01 · 比较两次实验</h2>
    <p className="muted">可从全部研究选择实验。表中保留两侧身份，跨研究也必须核对数据、时间区间与执行规则。改变选项会清除旧对比。</p>
    <div className="form-columns">
      <CatalogSelect<Run> label="基准实验" resource="runs" researchId="" value={baseline} onChange={(id) => select("baseline", id)} describe={describeRun} />
      <CatalogSelect<Run> label="候选实验" resource="runs" researchId="" value={candidate} onChange={(id) => select("candidate", id)} describe={describeRun} />
    </div>
    <button type="button" className="button primary" disabled={loading || !baseline || !candidate} onClick={() => void compare()}>{loading ? "正在读取对比" : "比较所选实验"}</button>
    {error && <p role="alert">{error}</p>}
    {result && <ComparisonResult result={result} />}
  </section>;
}

export function ComparisonResult({ result }: { result: RunComparison }) {
  return <div aria-label="已保存结果对比">
    <p role="status">{result.comparable ? "评估条件可比较；请分别核对各候选覆盖与来源。" : "存在不一致或未知条件；不生成指标差值。"}</p>
    <p className="fine-print">差值方向：候选实验减基准实验。数值从保存结果读取；界面不重新计算指标，不自动判断改善。读取时间：{formatDate(result.compared_at)}</p>
    <div className="table-scroll"><table><thead><tr><th>记录</th><th>基准实验</th><th>候选实验</th></tr></thead><tbody>
      <tr><th>实验</th><td>{result.baseline.run.id}</td><td>{result.candidate.run.id}</td></tr>
      <tr><th>研究 / 修订</th><td>{result.baseline.run.research_id}<br />{result.baseline.run.revision_id}</td><td>{result.candidate.run.research_id}<br />{result.candidate.run.revision_id}</td></tr>
      <tr><th>状态 / 流程</th><td><Badge status={result.baseline.run.status} /> · {modeName(result.baseline.run.mode)}</td><td><Badge status={result.candidate.run.status} /> · {modeName(result.candidate.run.mode)}</td></tr>
      <tr><th>源产物核验</th><td>{result.baseline.source_verified ? "已核验" : `未通过：${result.baseline.verification_error || "未记录"}`}</td><td>{result.candidate.source_verified ? "已核验" : `未通过：${result.candidate.verification_error || "未记录"}`}</td></tr>
      <tr><th>数据摘要</th><td>{result.baseline.dataset_sha256}</td><td>{result.candidate.dataset_sha256}</td></tr>
      <tr><th>工具调用 / 记录耗时（秒）</th><td>{insightValue(result.baseline.tool_calls)} / {insightValue(result.baseline.elapsed_seconds)}</td><td>{insightValue(result.candidate.tool_calls)} / {insightValue(result.candidate.elapsed_seconds)}</td></tr>
      <tr><th>完整配置与身份</th><td><RunSource value={result.baseline} label="查看基准配置与来源" /></td><td><RunSource value={result.candidate} label="查看候选配置与来源" /></td></tr>
    </tbody></table></div>
    <h3>条件检查</h3>
    <div className="table-scroll"><table><thead><tr><th>条件</th><th>状态</th><th>影响差值</th><th>基准</th><th>候选</th></tr></thead><tbody>{result.conditions.map((condition) => <tr key={condition.name}>
      <th>{conditionNames[condition.name] || condition.name}</th><td>{conditionStatus[condition.status]}</td><td>{condition.blocking ? "需满足" : "仅展示差异"}</td>
      <td><JsonDetails value={condition.baseline} label={`基准${conditionNames[condition.name] || condition.name}`} /></td><td><JsonDetails value={condition.candidate} label={`候选${conditionNames[condition.name] || condition.name}`} /></td>
    </tr>)}</tbody></table></div>
    <h3>候选表达式与指标</h3>
    {!result.candidates.length && <p>所选结果没有可列出的候选记录。</p>}
    {result.candidates.map((item) => <CandidateComparison key={item.candidate_id} item={item} conditionsComparable={result.comparable} />)}
    <h3>限制</h3><ul>{result.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul>
  </div>;
}

function RunSource({ value, label }: { value: ComparisonRun; label: string }) {
  return <JsonDetails value={value} label={label} />;
}

function CandidateComparison({ item, conditionsComparable }: { item: ComparisonCandidate; conditionsComparable: boolean }) {
  const comparable = conditionsComparable && item.comparable;
  return <article aria-label={`候选对比 ${item.candidate_id}`}>
    <h4>{item.candidate_id} · {comparable ? "可列指标差值" : "不列指标差值"}</h4>
    <p>覆盖：{{ both: "两侧均有记录", baseline_only: "仅基准实验有记录", candidate_only: "仅候选实验有记录" }[item.presence]}。表达式：{item.expression_changed === null ? "无法判断" : item.expression_changed ? "已变化" : "一致"}。</p>
    <div className="table-scroll"><table><thead><tr><th>记录</th><th>基准</th><th>候选</th></tr></thead><tbody>
      <tr><th>候选状态</th><td>{item.baseline_status ? statusName(item.baseline_status) : "未记录"}</td><td>{item.candidate_status ? statusName(item.candidate_status) : "未记录"}</td></tr>
      <tr><th>表达式</th><td><code>{insightValue(item.baseline_expression)}</code></td><td><code>{insightValue(item.candidate_expression)}</code></td></tr>
    </tbody></table></div>
    {!!item.reasons.length && <ul>{item.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
    {item.metrics.length ? <div className="table-scroll"><table><thead><tr><th>指标 / 覆盖</th><th>基准</th><th>候选</th><th>差值（候选 − 基准）</th><th>缺失或限制</th><th>产物来源</th></tr></thead><tbody>{item.metrics.map((metric) => <tr key={metric.name}>
      <th>{metric.name}</th><td>{insightValue(metric.baseline)}</td><td>{insightValue(metric.candidate)}</td><td>{metricDelta(metric.delta, comparable)}</td><td>{metric.reason || "—"}</td><td><JsonDetails label={`查看 ${metric.name} 的指标来源`} value={{ baseline: metric.baseline_source, candidate: metric.candidate_source }} /></td>
    </tr>)}</tbody></table></div> : <p>无可列指标；缺失值不会替换为零。</p>}
  </article>;
}

function ReportPanel({ workspaceId, research, selectedRun, busy, onPerform }: { workspaceId: string; research: Research; selectedRun: Run | null; busy: string; onPerform: Perform }) {
  const [pick, setPick] = useState<Run | null>(null);
  const [selected, setSelected] = useState<Run[]>([]);
  const [selectionError, setSelectionError] = useState("");
  const [notice, setNotice] = useState("");
  const [list, setList] = useState<ReportSummary[]>([]);
  const [pageSize, setPageSize] = useState(20);
  const pageSizeRef = useRef(20);
  const [total, setTotal] = useState(0);
  const [nextOffset, setNextOffset] = useState(0);
  const [listError, setListError] = useState("");
  const [listLoading, setListLoading] = useState(false);
  const [detail, setDetail] = useState<ResearchReport | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState("");
  const mounted = useRef(true);
  const listScope = useRef(new InsightRequestEpoch());
  const detailScope = useRef(new InsightRequestEpoch());
  const mutation = useRecoverableMutation<ResearchReport>(recoveryKey(workspaceId, "research-report-request", research.id), `/researches/${research.id}/reports`);
  const blocked = !!busy || !!mutation.pending || mutation.blocked || !mutation.ready;
  useEffect(() => {
    mounted.current = true;
    void loadList(0).catch(() => undefined);
    return () => { mounted.current = false; listScope.current.invalidate(); detailScope.current.invalidate(); };
    // The parent keys this component by workspace and research, avoiding scope reuse.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  async function loadList(offset: number) {
    const token = listScope.current.begin();
    if (mounted.current) { setListLoading(true); setListError(""); }
    try {
      const response = await api<ReportList>(`/researches/${research.id}/reports?limit=${pageSizeRef.current}&offset=${offset}`);
      if (!mounted.current || !listScope.current.isCurrent(token)) return;
      setList((old) => offset ? [...new Map([...old, ...response.items].map((item) => [item.id, item])).values()] : response.items);
      setTotal(response.total); setNextOffset(response.offset + response.items.length);
    } catch (e) {
      if (!mounted.current || !listScope.current.isCurrent(token)) return;
      setListError(e instanceof ApiError && e.status === 413 ? `本页报告超过读取大小限制，请将每页数量调小（可选 1 项）后重新读取。${describeError(e)}` : describeError(e));
      throw e;
    } finally { if (mounted.current && listScope.current.isCurrent(token)) setListLoading(false); }
  }
  async function showReport(id: string) {
    const token = detailScope.current.begin();
    setDetail(null); setDetailLoading(id); setDetailError("");
    try {
      const response = await api<ResearchReport>(`/research-reports/${encodeURIComponent(id)}`);
      if (mounted.current && detailScope.current.isCurrent(token)) setDetail(response);
    } catch (e) { if (mounted.current && detailScope.current.isCurrent(token)) setDetailError(describeError(e)); }
    finally { if (mounted.current && detailScope.current.isCurrent(token)) setDetailLoading(""); }
  }
  function add() {
    if (!pick) return;
    try { setSelected(addReportRun(selected, pick, research.id)); setPick(null); setSelectionError(""); }
    catch (e) { setSelectionError(describeError(e)); }
  }
  async function create(body?: Record<string, unknown>) {
    await onPerform(body ? "生成冻结研究总结" : "安全确认研究总结", async () => {
      if (mounted.current) setNotice("");
      const report = await mutation.execute(body);
      let localWarning = "";
      // There is no persistent pre-submit draft; only the frozen request persists.
      // Keep it pinned when acknowledgement fails so another click cannot create a second report.
      try {
        mutation.acknowledge();
        if (mounted.current) { setSelected([]); setPick(null); }
      } catch (e) { localWarning = `本机请求尚未清理；请用原请求安全重试完成确认。${describeError(e)}`; }
      if (!mounted.current) return;
      detailScope.current.invalidate(); setDetailLoading(""); setDetailError(""); setDetail(report);
      setNotice(`研究总结已保存。${localWarning}`);
      try { await loadList(0); }
      catch (e) { if (mounted.current) setNotice(`研究总结已保存，列表刷新暂未成功；固定报告仍可下载。${describeError(e)}${localWarning}`); }
    });
  }
  return <section className="panel" aria-label="冻结研究总结">
    <h2>生成冻结研究总结</h2>
    <section aria-label="当前实验报告范围">
      {selectedRun && selectedRun.research_id === research.id ? <p>本次范围：仅当前实验 {formatDate(selectedRun.created_at)} · {shortId(selectedRun.id)} · {revisionLabel(research, selectedRun.revision_id)} · {statusName(selectedRun.status)}。不会自动加入其他实验或待提交修订。</p> : <p>尚未选择当前研究的实验。请先从实验记录选择，或展开下方高级范围。</p>}
      {selectedRun && selectedRun.research_id === research.id && <JsonDetails value={{research_id:research.id, run_id:selectedRun.id, revision_id:selectedRun.revision_id}} label="查看当前报告范围的完整标识" />}
      <button type="button" className="button primary" disabled={blocked || !selectedRun || selectedRun.research_id !== research.id || activeStatuses.has(selectedRun.status)}
        onClick={() => void create(currentRunReport(research.id, selectedRun))}>为当前实验生成报告</button>
      <p className="fine-print">点击后才创建报告。审核或计时未记录会如实保留，不要求先进行流程观测。</p>
    </section>
    <p>显式选择当前研究的 1–{reportRunLimit} 项实验。报告会保留研究修订及所选实验的证据、候选、尝试、失败、审核和相关问题记录；所选范围不代表全部历史。</p>
    {notice && <p role="status">{notice}</p>}
    {mutation.error && <p role="alert">{mutation.error}</p>}
    {mutation.pending && <section aria-label="研究总结提交恢复" className="info-note">
      <p>{mutation.pending.state === "rejected" ? "研究总结请求已明确拒绝，可以解除后重新选择。" : "研究总结提交结果待确认。请使用原请求安全重试，不会生成新的请求标识。"}</p>
      <JsonDetails value={mutation.pending.body} label="查看冻结总结请求" />
      {mutation.pending.state === "rejected" ? <button className="button" disabled={!!busy} onClick={mutation.dismissRejected}>解除被拒绝的总结请求</button> : <button className="button" disabled={!!busy} onClick={() => void create()}>安全重试研究总结</button>}
    </section>}
    <details><summary>高级：自选多次实验的报告范围</summary>
    <fieldset disabled={blocked}>
      <CatalogSelect<Run> label="加入总结的实验" resource="runs" researchId={research.id} value={pick?.id || ""} onChange={(_id, item) => setPick(item || null)} describe={describeRun} seed={selected} />
      <button className="button small" type="button" disabled={!pick || selected.length >= reportRunLimit} onClick={add}>加入总结范围</button>
      {selectionError && <p role="alert">{selectionError}</p>}
      <p>已明确选择 {selected.length} / {reportRunLimit} 项实验。</p>
      {!!selected.length && <ul aria-label="总结实验范围">{selected.map((run) => <li key={run.id}>{run.id} · {statusName(run.status)} · 修订 {shortId(run.revision_id)} <button type="button" className="button small" onClick={() => setSelected((old) => old.filter((item) => item.id !== run.id))}>移除 {shortId(run.id)}</button></li>)}</ul>}
      <button type="button" className="button primary" disabled={!selected.length} onClick={() => void create({ run_ids: selected.map((run) => run.id) })}>生成冻结研究总结</button>
    </fieldset>
    </details>
    <p className="fine-print">提交前会将固定请求保存到本机；本机存储不可用时不会发送。后续新增审核不改写旧报告，需要新记录时请明确生成新的总结。完整性校验不代表研究判断已通过。</p>
    <h3>已保存总结</h3>
    <Field label="总结列表每页数量" hint="报告内容有读取大小限制；较大报告可选每页 1 项。改变数量会从第一页重新读取。"><select value={pageSize} onChange={(e) => {
      const value = Number(e.target.value); pageSizeRef.current = value; setPageSize(value);
      setList([]); setTotal(0); setNextOffset(0);
      void loadList(0).catch(() => undefined);
    }}>{[1, 5, 20].map((value) => <option key={value} value={value}>{value} 项</option>)}</select></Field>
    <div className="button-row"><button className="button small" type="button" disabled={listLoading} onClick={() => void loadList(0).catch(() => undefined)}>刷新总结列表</button>{nextOffset < total && <button className="button small" type="button" disabled={listLoading} onClick={() => void loadList(nextOffset).catch(() => undefined)}>加载更多总结</button>}</div>
    <p className="fine-print">已加载 {list.length} 项，总计 {total} 项。列表按保存时间展示，刷新可读取新记录。</p>
    {listError && <p role="alert">{listError}</p>}
    <div className="table-scroll"><table><thead><tr><th>固定报告</th><th>保存时间 / 范围</th><th>内容摘要</th><th>查看与下载</th></tr></thead><tbody>{list.map((report) => <tr key={report.id}>
      <td>{report.id}</td><td>{formatDate(report.created_at)}<br />{report.selected_run_ids.length} 项已选择实验</td><td><code>{report.payload_digest}</code><br />快照内容完整性已校验</td>
      <td><button type="button" className="button small" disabled={detailLoading === report.id} onClick={() => void showReport(report.id)}>查看总结 {shortId(report.id)}</button><ReportDownloads report={report} /></td>
    </tr>)}</tbody></table></div>
    {detailError && <p role="alert">{detailError}</p>}
    {detail && <article aria-label="固定研究总结详情">
      <h3>固定总结 {detail.id}</h3>
      <p>研究：{detail.research_id} · 保存于 {formatDate(detail.created_at)}。内容摘要：{detail.payload_digest}。</p>
      <ReportDownloads report={detail} />
      <JsonDetails value={{ selected_run_ids: detail.selected_run_ids, counts: detail.counts }} label="查看总结范围与记录计数" />
      <JsonDetails value={detail.payload} label="查看冻结研究记录（含来源与限制）" />
    </article>}
  </section>;
}

function ReportDownloads({ report }: { report: ReportSummary }) {
  const path = `/api/research-reports/${encodeURIComponent(report.id)}/export`;
  return <div className="button-row"><a className="button small" href={`${path}?format=json`} download aria-label={`下载 JSON 总结 ${report.id}`}>下载 JSON</a><a className="button small" href={`${path}?format=markdown`} download aria-label={`下载 Markdown 总结 ${report.id}`}>下载 Markdown</a></div>;
}
