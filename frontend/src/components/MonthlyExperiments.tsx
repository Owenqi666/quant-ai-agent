import { useCallback, useEffect, useRef, useState } from "react";
import { api, post } from "../api";
import { describeError, formatDate } from "../domain";
import type { MonthlyAck, MonthlyConfig, MonthlyDefaults, MonthlyDetail, MonthlyPage, MonthlyReport, MonthlyResult, MonthlyReview, MonthlyStatus, ProtocolPage, ProtocolRecord } from "../generated/api-contract";
import { monthlyActive, monthlyCanRetry, monthlyConfigError, monthlyNumber, monthlyReviewTarget, monthlyStateNames, monthlyStrategyNames } from "../monthlyExperiments";
import { recoveryKey } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import type { WorkspaceRequestScope } from "../workspaceScope";
import MutationRecovery from "./MutationRecovery";
import { Field, JsonDetails } from "./ui";

const base = "/monthly-experiments";
const name = (status: string) => monthlyStateNames[status] || status;

/** Independent monthly resources; no daily research or daily experiment selection. */
export default function MonthlyExperiments({ workspaceScope, onOpenProtocols, initialId = "" }: {
  workspaceScope: WorkspaceRequestScope; onOpenProtocols: () => void; initialId?: string;
}) {
  const [defaults, setDefaults] = useState<MonthlyDefaults | null>(null);
  const [config, setConfig] = useState<MonthlyConfig | null>(null);
  const [protocols, setProtocols] = useState<ProtocolPage | null>(null);
  const [protocol, setProtocol] = useState<ProtocolRecord | null>(null);
  const [protocolOffset, setProtocolOffset] = useState(0);
  const [page, setPage] = useState<MonthlyPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState(initialId);
  const [confirmed, setConfirmed] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [listError, setListError] = useState("");
  const [protocolError, setProtocolError] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(false);
  const mutation = useRecoverableMutation<MonthlyAck>(recoveryKey(workspaceScope.workspaceId, "monthly-create-request"), base);
  const changed = useCallback(() => setRefresh(value => value + 1), []);
  useEffect(() => {
    mounted.current = true;
    const workspace = workspaceScope.capture(), abort = new AbortController();
    void api<MonthlyDefaults>(`${base}/defaults`, { signal: abort.signal }).then(value => {
      if (mounted.current && workspaceScope.isCurrent(workspace, abort.signal)) { setDefaults(value); setConfig(value.config); }
    }).catch(e => { if (workspaceScope.isCurrent(workspace, abort.signal)) setError(describeError(e)); });
    return () => { mounted.current = false; abort.abort(); };
  }, [workspaceScope]);
  useEffect(() => {
    const workspace = workspaceScope.capture(), abort = new AbortController();
    setProtocolError(""); setProtocols(null);
    void api<ProtocolPage>(`/research-protocols?limit=20&offset=${protocolOffset}`, { signal: abort.signal }).then(value => {
      if (workspaceScope.isCurrent(workspace, abort.signal)) setProtocols(value);
    }).catch(e => { if (workspaceScope.isCurrent(workspace, abort.signal)) setProtocolError(describeError(e)); });
    return () => abort.abort();
  }, [workspaceScope, protocolOffset, refresh]);
  useEffect(() => {
    const workspace = workspaceScope.capture(), abort = new AbortController();
    setListError(""); setPage(null);
    void api<MonthlyPage>(`${base}?limit=20&offset=${offset}`, { signal: abort.signal }).then(value => {
      if (!workspaceScope.isCurrent(workspace, abort.signal)) return;
      setPage(value); setSelected(previous => previous || value.items[0]?.id || "");
    }).catch(e => { if (workspaceScope.isCurrent(workspace, abort.signal)) setListError(describeError(e)); });
    return () => abort.abort();
  }, [workspaceScope, offset, refresh]);
  async function submit(body?: Record<string, unknown>) {
    const workspace = workspaceScope.capture(); setSending(true); setError(""); setNotice("");
    try {
      const saved = await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(workspace)) return;
      try { mutation.acknowledge(); }
      catch (e) { setNotice(`月度实验已保存；本机确认未完成，请用原请求安全重试。${describeError(e)}`); return; }
      setNotice(`月度实验已保存：${saved.experiment_id}。列表读取失败也不会重新提交。`);
      setSelected(saved.experiment_id); setConfirmed(false); setOffset(0); changed();
    } catch (e) { if (mounted.current && workspaceScope.isCurrent(workspace)) setError(describeError(e)); }
    finally { if (mounted.current && workspaceScope.isCurrent(workspace)) setSending(false); }
  }
  const invalid = monthlyConfigError(config);
  const locked = sending || !!mutation.pending || mutation.blocked || !mutation.ready;
  return <>
    <section className="panel" aria-label="月度实验提交">
      <h2>选择已保存规则，提交受控月度实验</h2>
      <p>独立月度流程：共同形成样本 → MOM 基线与 MOM × ID 对照 → 完整持有期标签 → 审核与冻结报告。AI 未启用。</p>
      <p>MOM × ID 表示双维度分组后的低 ID 动量减高 ID 动量，不是直接相乘两个信号。</p>
      <p className="info-note">仅使用服务器生成的 controlled_fixture 虚构日收益和日历。组合定义是项目比较约定；结果不代表真实市场收益、论文复现或投资有效性。代理成本和净值忽略持仓漂移、融资、借券及滑点。</p>
      {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      <MutationRecovery name="月度实验" pending={mutation.pending} error={mutation.error} busy={sending}
        onRetry={() => void submit()} onDismiss={mutation.dismissRejected} />
      {protocolError && <p role="alert">{protocolError}</p>}
      {protocols?.total === 0 && <p>尚无保存规则。先保存明确的研究规则版本，再回到这里提交实验。</p>}
      <div className="button-row"><button className="button" onClick={onOpenProtocols}>前往研究规则</button>
        <button className="button small" onClick={changed}>刷新月度列表与规则</button></div>
      <fieldset disabled={locked}>
        <Field label="月度实验规则版本"><select value={protocol?.id || ""} onChange={e => { setProtocol(protocols?.items.find(item => item.id === e.target.value) || null); setConfirmed(false); }}>
          <option value="">请选择已保存规则</option>
          {protocol && !protocols?.items.some(item => item.id === protocol.id) && <option value={protocol.id}>{protocol.title} · {protocol.id.slice(-8)}</option>}
          {protocols?.items.map(item => <option key={item.id} value={item.id}>{item.title} · {item.config.mode === "paper" ? "原文未决" : "项目约定"} · {item.id.slice(-8)}</option>)}
        </select></Field>
        <div className="button-row"><button className="button small" disabled={!protocolOffset} onClick={() => setProtocolOffset(value => Math.max(0, value - 20))}>上一页月度规则</button>
          <button className="button small" disabled={!protocols || protocolOffset + protocols.items.length >= protocols.total} onClick={() => setProtocolOffset(value => value + 20)}>下一页月度规则</button></div>
        {protocol && <><p>{protocol.title} · 配置摘要 {protocol.config_digest}</p>
          {protocol.config.mode === "paper" && <p className="info-note warning">原文规则仍有未决项。实验可记录 blocked 结果，不能自动填补规则并声称已完成论文复现。</p>}
          <JsonDetails value={protocol} label="检查冻结规则、依据与修改记录" /></>}
        {config && <div className="form-columns">
          <Field label="月度开始月份"><input type="month" min="1905-01" max="2099-12" value={config.start_month} onChange={e => { setConfig({ ...config, start_month: e.target.value }); setConfirmed(false); }} /></Field>
          <Field label="月度结束月份"><input type="month" min="1905-01" max="2099-12" value={config.end_month} onChange={e => { setConfig({ ...config, end_month: e.target.value }); setConfirmed(false); }} /></Field>
          <Field label="代理成本 bps"><input type="number" min={0} max={100} step="any" value={Number.isFinite(config.cost_bps) ? config.cost_bps : ""} onChange={e => { setConfig({ ...config, cost_bps: e.target.value === "" ? NaN : Number(e.target.value) }); setConfirmed(false); }} /></Field>
          <Field label="形成样本资产下限"><input type="number" min={9} max={128} step={1} value={Number.isFinite(config.min_assets) ? config.min_assets : ""} onChange={e => { setConfig({ ...config, min_assets: e.target.value === "" ? NaN : Number(e.target.value) }); setConfirmed(false); }} /></Field>
        </div>}
        {invalid && <p role="alert">{invalid}</p>}
        <Field label="已确认规则、月份、受控数据及代理成本限制"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /></Field>
        <button className="button primary" disabled={!protocol || !config || !!invalid || !confirmed} onClick={() => {
          if (protocol && config) void submit({ protocol_id: protocol.id, protocol_digest: protocol.digest, config });
        }}>提交月度实验</button>
      </fieldset>
      {defaults && <JsonDetails value={defaults} label="受控数据来源与默认配置" />}
    </section>
    <section className="panel" aria-label="月度实验历史"><h2>月度实验历史</h2>
      {listError && <p role="alert">月度目录读取失败；已保存实验保持原记录。{listError}</p>}
      <div className="button-row"><button className="button small" onClick={changed}>刷新月度实验历史</button>
        <button className="button small" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 20))}>上一页月度实验</button>
        <button className="button small" disabled={!page || offset + page.items.length >= page.total} onClick={() => setOffset(value => value + 20)}>下一页月度实验</button></div>
      <p>{page ? `本页 ${page.items.length} / 共 ${page.total} 项；从第 ${offset + 1} 项开始。` : "正在读取月度目录。"}</p>
      <div className="table-scroll"><table><thead><tr><th>实验</th><th>月份 / 规则</th><th>流程状态</th><th>尝试 / 更新时间</th></tr></thead><tbody>{page?.items.map(item => <tr key={item.id}>
        <td><button className="text-button" aria-label={`查看月度实验 ${item.id}`} onClick={() => setSelected(item.id)}>{item.id}</button></td>
        <td>{item.config.start_month} → {item.config.end_month}<br />{item.protocol_id}</td><td>{name(item.status)}</td><td>{item.attempt_count} / {formatDate(item.updated_at)}</td>
      </tr>)}</tbody></table></div>
    </section>
    {selected && <MonthlyExperimentDetail key={`${workspaceScope.key}:${selected}`} id={selected} workspaceScope={workspaceScope} onChanged={changed} />}
  </>;
}

function MonthlyExperimentDetail({ id, workspaceScope, onChanged }: { id: string; workspaceScope: WorkspaceRequestScope; onChanged: () => void }) {
  const [detail, setDetail] = useState<MonthlyDetail | null>(null);
  const [status, setStatus] = useState<MonthlyStatus | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(false), detailEpoch = useRef(0), token = useRef<string | null>(null);
  const currentDetail = useRef(detail); currentDetail.current = detail;
  const detailAbort = useRef<AbortController | null>(null);
  const retry = useRecoverableMutation<MonthlyAck>(recoveryKey(workspaceScope.workspaceId, "monthly-retry-request", id), `${base}/${id}/retry`);
  const loadDetail = useCallback(async () => {
    detailAbort.current?.abort(); const abort = new AbortController(); detailAbort.current = abort;
    const epoch = ++detailEpoch.current, workspace = workspaceScope.capture();
    const current = () => mounted.current && epoch === detailEpoch.current && workspaceScope.isCurrent(workspace, abort.signal);
    setLoading(true); setError("");
    try { const value = await api<MonthlyDetail>(`${base}/${id}`, { signal: abort.signal }); if (current()) setDetail(value); }
    catch (e) { if (current()) { setDetail(null); setError(`完整结果读取或核验失败；旧结果已隐藏。${describeError(e)}`); } }
    finally { if (current()) setLoading(false); }
  }, [id, workspaceScope]);
  useEffect(() => {
    mounted.current = true; const workspace = workspaceScope.capture(), abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    void loadDetail();
    async function poll() {
      let delay = 5000;
      try {
        const next = await api<MonthlyStatus>(`${base}/${id}/status`, { signal: abort.signal });
        if (!mounted.current || !workspaceScope.isCurrent(workspace, abort.signal)) return;
        setStatus(next); delay = monthlyActive(next.status) ? 1500 : 5000;
        const visible = currentDetail.current?.experiment;
        const changedTarget = !visible || visible.attempt_id !== next.attempt_id || visible.status !== next.status;
        const needsDetail = token.current !== next.change_token && (token.current !== null || changedTarget);
        // Remember an attempted token even when full verification fails. A stable
        // terminal source is retried only by the explicit refresh control.
        token.current = next.change_token;
        if (needsDetail) {
          if (changedTarget) setDetail(null);
          await loadDetail();
        }
      } catch (e) { if (mounted.current && workspaceScope.isCurrent(workspace, abort.signal)) setError(`状态读取暂未成功。${describeError(e)}`); }
      if (mounted.current && workspaceScope.isCurrent(workspace, abort.signal)) timer = setTimeout(() => void poll(), delay);
    }
    timer = setTimeout(() => void poll(), 750);
    return () => { mounted.current = false; abort.abort(); clearTimeout(timer); detailAbort.current?.abort(); };
  }, [id, workspaceScope, loadDetail]);
  async function action(kind: "cancel" | "retry", body?: Record<string, unknown>) {
    const workspace = workspaceScope.capture(); setBusy(true); setError(""); setNotice("");
    try {
      if (kind === "retry") await retry.execute(body);
      else await post<MonthlyAck>(`${base}/${id}/cancel`, { expected_attempt_id: detail?.experiment.attempt_id ?? null });
      if (!mounted.current || !workspaceScope.isCurrent(workspace)) return;
      if (kind === "retry") {
        try { retry.acknowledge(); }
        catch (e) { setNotice(`重试操作已保存，本机确认未完成。${describeError(e)}`); return; }
      }
      setNotice(kind === "retry" ? "月度重试已保存；新尝试使用原冻结输入。" : "取消请求已确认；终态以服务记录为准。");
      await loadDetail(); onChanged();
    } catch (e) { if (mounted.current && workspaceScope.isCurrent(workspace)) setError(describeError(e)); }
    finally { if (mounted.current && workspaceScope.isCurrent(workspace)) setBusy(false); }
  }
  const experiment = detail?.experiment;
  return <section className="panel" aria-label="月度实验详情" data-experiment-id={id}>
    <h2>月度实验 {id}</h2>
    <p>流程状态：{name(status?.status || experiment?.status || "正在读取")} · 阶段 {status?.phase || experiment?.phase || "未记录"}。完成状态只表示流水线完成；研究计算状态与来源核验分别显示。</p>
    <p className="fine-print">状态轮询不核验文件。下方结果来自最近一次完整核验；可随时显式重新检查。</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <MutationRecovery name="月度重试" pending={retry.pending} error={retry.error} busy={busy} onRetry={() => void action("retry")} onDismiss={retry.dismissRejected} />
    <div className="button-row"><button className="button" disabled={loading} onClick={() => void loadDetail()}>完整核验并刷新月度结果</button>
      {experiment && monthlyActive(experiment.status) && <button className="button" disabled={busy || loading} onClick={() => void action("cancel")}>取消月度实验</button>}
      {experiment && monthlyCanRetry(experiment) && <button className="button" disabled={busy || loading || !!retry.pending || retry.blocked || !retry.ready} onClick={() => void action("retry", { expected_attempt_id: experiment.attempt_id })}>重试月度实验</button>}</div>
    {experiment?.status === "cancelled" && !experiment.attempt_id && <p>此实验在首次执行前取消，没有可重试的尝试；如需运行，请在上方明确提交新实验。</p>}
    {loading && <p role="status">正在读取并完整核验月度实验。</p>}
    {detail && <>
      {experiment?.error && <p role="alert">运行错误：{experiment.error}</p>}
      <p>规则：{detail.protocol.title} · {detail.protocol.config.mode === "paper" ? "原文未决" : "项目约定"}。当前尝试 {experiment?.attempt_id || "尚未开始"}；共 {experiment?.attempt_count} 次。</p>
      <p>源产物核验：{detail.verification.verified ? "通过" : "尚无通过核验的结果"}；独立数值参考：{detail.verification.reference_passed === null ? "不适用 / 未提供" : detail.verification.reference_passed ? "通过" : "未通过"}。</p>
      <JsonDetails value={{ protocol: detail.protocol, experiment, verification: detail.verification, attempts: detail.attempts }} label="冻结输入身份、运行错误与全部尝试" />
      {detail.result && detail.verification.verified && <MonthlyResultView result={detail.result} />}
    </>}
    <MonthlyResultActions key={id} id={id} detail={detail} workspaceScope={workspaceScope} onSaved={loadDetail} />
  </section>;
}

export function MonthlyResultView({ result }: { result: MonthlyResult }) {
  return <section aria-label="月度计算结果">
    <h3>计算状态：{name(result.status)}</h3>
    <p>{result.data_kind} · {result.source_id} · {result.semantics_version}。这里只显示服务保存的结果，不在浏览器重新计算统计。</p>
    <p>持有期标签要求完整观察。缺失不补零，不按未来标签筛掉持仓或重新归一化；真实零收益保留。净收益、净值、回撤和 Sharpe 均采用目标权重成本代理。</p>
    {!!result.warnings.length && <ul>{result.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul>}
    <div className="table-scroll"><table><thead><tr><th>策略</th><th>有效 gross / net / 换手月份；总月份</th><th>均值 gross / net proxy</th><th>年化波动 / Sharpe proxy</th><th>期末 NAV / 最大回撤 proxy</th><th>平均换手 proxy / 完整路径</th></tr></thead><tbody>{result.summary.map(metric => <tr key={metric.strategy_id}>
      <th>{monthlyStrategyNames[metric.strategy_id]}</th><td>{metric.gross_months} / {metric.months_evaluated} / {metric.turnover_months}；共 {metric.months_total}</td>
      <td>{monthlyNumber(metric.mean_gross_return, true)} / {monthlyNumber(metric.mean_net_return_proxy, true)}</td>
      <td>{monthlyNumber(metric.volatility_annualized_proxy, true)} / {monthlyNumber(metric.sharpe_annualized_proxy)}</td>
      <td>{monthlyNumber(metric.terminal_nav_proxy)} / {monthlyNumber(metric.max_drawdown_proxy, true)}</td>
      <td>{monthlyNumber(metric.mean_turnover_proxy, true)} / {metric.cumulative_complete ? "完整" : "不完整，不补接"}</td>
    </tr>)}</tbody></table></div>
    {result.months.map(month => <details key={month.month} aria-label={`月度明细 ${month.month}`}>
      <summary>{month.month} · {name(month.status)} · 形成时点 {month.as_of} · 合资格 {month.eligible_assets.length} 项资产</summary>
      <p>对照减基线 net proxy：{monthlyNumber(month.difference_net_proxy, true)}。缺少任一侧时不提供差值。</p>
      <JsonDetails value={month.windows} label={`${month.month} 信号窗口`} />
      <div className="table-scroll"><table><thead><tr><th>策略 / 状态</th><th>gross / net proxy</th><th>交易权重 / 换手 proxy</th><th>成本 / NAV / 回撤 proxy</th><th>不可用原因</th></tr></thead><tbody>{month.strategies.map(strategy => <tr key={strategy.id}>
        <th>{monthlyStrategyNames[strategy.id]} / {name(strategy.status)}</th><td>{monthlyNumber(strategy.gross_return, true)} / {monthlyNumber(strategy.net_return_proxy, true)}</td>
        <td>{monthlyNumber(strategy.traded_weight)} / {monthlyNumber(strategy.turnover_proxy)}</td><td>{monthlyNumber(strategy.estimated_cost, true)} / {monthlyNumber(strategy.nav_proxy)} / {monthlyNumber(strategy.drawdown_proxy, true)}</td><td>{strategy.reasons.join("；") || "无"}</td>
      </tr>)}</tbody></table></div>
      <details><summary>{month.month} 形成权重与信号分组</summary>
        {month.strategies.map(strategy => <div key={strategy.id}><h4>{monthlyStrategyNames[strategy.id]}：形成权重</h4><div className="table-scroll"><table><thead><tr><th>资产</th><th>目标权重</th></tr></thead><tbody>{strategy.weights.map(weight => <tr key={weight.asset}><td>{weight.asset}</td><td>{monthlyNumber(weight.weight, true)}</td></tr>)}</tbody></table></div>{!strategy.weights.length && <p>本月未形成权重。</p>}</div>)}
        <JsonDetails value={month.signals} label={`${month.month} MOM / ID 与三分组`} /><JsonDetails value={month.groups} label={`${month.month} 九格诊断收益`} />
      </details>
      <details><summary>{month.month} 标签覆盖与排除原因</summary>
        <div className="table-scroll"><table><thead><tr><th>资产</th><th>持有期收益</th><th>有效 / 应有日</th><th>缺行 / 空值 / 零</th><th>标签不可用原因</th></tr></thead><tbody>{month.labels.map(label => <tr key={label.asset}><td>{label.asset}</td><td>{monthlyNumber(label.return_value, true)}</td><td>{label.coverage.valid} / {label.coverage.expected}</td><td>{label.coverage.missing_rows} / {label.coverage.null_values} / {label.coverage.zero}</td><td>{label.reasons.join("；") || "完整标签"}</td></tr>)}</tbody></table></div>
        <JsonDetails value={month.exclusions} label={`${month.month} 形成时点排除清单`} /><JsonDetails value={month.labels} label={`${month.month} 完整标签覆盖与缺失日期`} />
      </details>
      {!!month.warnings.length && <ul>{month.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul>}
    </details>)}
    <JsonDetails value={{ config: result.config, rules: result.rules, config_digest: result.config_digest, input_digest: result.input_digest }} label="月度结果配置与摘要" />
  </section>;
}

function MonthlyResultActions({ id, detail, workspaceScope, onSaved }: { id: string; detail: MonthlyDetail | null; workspaceScope: WorkspaceRequestScope; onSaved: () => Promise<void> }) {
  const [verdict, setVerdict] = useState<MonthlyReview["verdict"]>("needs_revision");
  const [note, setNote] = useState("");
  const [source, setSource] = useState<MonthlyReview["source"]>("human");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [savedReport, setSavedReport] = useState<MonthlyReport | null>(null);
  const mounted = useRef(false);
  const knownReports = useRef<MonthlyDetail["reports"]>([]);
  if (detail) knownReports.current = detail.reports;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const review = useRecoverableMutation<MonthlyReview>(recoveryKey(workspaceScope.workspaceId, "monthly-review-request", id), `${base}/${id}/reviews`);
  const report = useRecoverableMutation<MonthlyReport>(recoveryKey(workspaceScope.workspaceId, "monthly-report-request", id), `${base}/${id}/reports`);
  const target = monthlyReviewTarget(detail);
  async function save(kind: "review" | "report", body?: Record<string, unknown>) {
    const workspace = workspaceScope.capture(); setBusy(kind); setError(""); setNotice("");
    try {
      const mutation = kind === "review" ? review : report;
      const saved = await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(workspace)) return;
      let warning = "";
      try { mutation.acknowledge(); if (kind === "review") setNote(""); }
      catch (e) { warning = `本机确认未完成，原请求已保留，请安全重试。${describeError(e)}`; }
      if (kind === "report") setSavedReport(saved as MonthlyReport);
      setNotice(`${kind === "review" ? "月度审核" : "冻结月度报告"}已保存。${warning}`);
      await onSaved();
    } catch (e) { if (mounted.current && workspaceScope.isCurrent(workspace)) setError(describeError(e)); }
    finally { if (mounted.current && workspaceScope.isCurrent(workspace)) setBusy(""); }
  }
  return <section aria-label="月度审核与报告">
    <h3>审核确切结果，保存冻结报告</h3>
    <p>审核只是声明的研究判断；接受不代表投资有效。报告保存当时结果和审核，后续审核不改变旧报告。</p>
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
    <MutationRecovery name="月度审核" pending={review.pending} error={review.error} busy={!!busy} onRetry={() => void save("review")} onDismiss={review.dismissRejected} />
    <MutationRecovery name="月度报告" pending={report.pending} error={report.error} busy={!!busy} onRetry={() => void save("report")} onDismiss={report.dismissRejected} />
    {!target && <p>当前尝试尚无完成且通过核验的确切结果。只能确认此前已经发送的原请求。</p>}
    <fieldset disabled={!target || !!busy || !!review.pending || review.blocked || !review.ready}>
      <Field label="月度审核结论"><select value={verdict} onChange={e => setVerdict(e.target.value as MonthlyReview["verdict"])}><option value="needs_revision">需要修改</option><option value="accepted">接受</option><option value="rejected">拒绝</option></select></Field>
      <Field label="月度审核声明来源"><select value={source} onChange={e => setSource(e.target.value as MonthlyReview["source"])}><option value="human">人工声明</option><option value="automation">自动验收样例</option></select></Field>
      <Field label="月度审核依据"><textarea rows={3} maxLength={4000} value={note} onChange={e => setNote(e.target.value)} /></Field>
      <button className="button primary" disabled={!note.trim()} onClick={() => target && void save("review", { ...target, verdict, note: note.trim(), source })}>保存月度审核</button>
    </fieldset>
    <button className="button" disabled={!target || !!busy || !!report.pending || report.blocked || !report.ready} onClick={() => target && void save("report", { ...target })}>冻结月度报告</button>
    <JsonDetails value={target} label="月度审核与报告的冻结目标" />
    <JsonDetails value={detail?.reviews || []} label="已有月度审核记录" />
    <h4>固定报告下载</h4>
    <p className="fine-print">固定报告按自己的摘要独立核验；源实验后来不可读取时，已知报告身份与原请求仍可用于确认和下载。</p>
    {savedReport && <><p>刚保存的报告 {savedReport.id} · {savedReport.digest}</p><MonthlyDownloads id={id} reportId={savedReport.id} /><JsonDetails value={savedReport} label="刚保存的冻结月度报告" /></>}
    {knownReports.current.map(item => <article key={item.id}><p>{item.id} · {formatDate(item.created_at)} · 尝试 {item.attempt_id} · 摘要 {item.digest}</p><MonthlyDownloads id={id} reportId={item.id} /></article>)}
    {!knownReports.current.length && !savedReport && <p>尚未读取到冻结月度报告。</p>}
  </section>;
}
function MonthlyDownloads({ id, reportId }: { id: string; reportId: string }) {
  const path = `/api${base}/${encodeURIComponent(id)}/reports/${encodeURIComponent(reportId)}`;
  return <div className="button-row"><a className="button small" href={`${path}/json`} download aria-label={`下载月度 JSON ${reportId}`}>下载月度 JSON</a><a className="button small" href={`${path}/markdown`} download aria-label={`下载月度 Markdown ${reportId}`}>下载月度 Markdown</a></div>;
}
