import {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {api} from "../api";
import {describeError, formatDate} from "../domain";
import type {CaseDetail, CasePreview, IndustryMomAck, IndustryMomExperimentDetail, IndustryMomExperimentPage,
  IndustryMomSourceDetail, IndustryMomSourcePage, IndustryMomStatus} from "../generated/api-contract";
import type {WorkspaceRequestScope} from "../workspaceScope";
import {useRecoverableMutation} from "../useRecoverableMutation";
import {recoveryKey} from "../reviewRecovery";
import {caseCreateBody} from "../researchCases";
import {industryActive, industryCanRetry, industryNumber, industryReportPath, industryStateNames,
  industryStrategyNames, industryVerifiedResult} from "../industryMomExperiments";
import type {IndustryMomResult} from "../industryMomExperiments";
import MutationRecovery from "./MutationRecovery";
import {Field, JsonDetails} from "./ui";

const base = "/industry-mom-experiments";
const stateName = (status: string) => industryStateNames[status] || status;
const scopeNote = "真实来源是 49 个预计算行业组合，固定 2010–2011 开发区间；不是个股或原论文复现。候选净敞口 0，基线净敞口 1，收益差不能解释为同风险 Alpha 优势。只算毛收益，未建模费用、融资、借券和成交。";

export default function IndustryMomExperiments({workspaceScope, initialId = "", onOpenCase}: {
  workspaceScope: WorkspaceRequestScope; initialId?: string; onOpenCase: (id: string) => void;
}) {
  const [sources, setSources] = useState<IndustryMomSourcePage | null>(null), [source, setSource] = useState<IndustryMomSourceDetail | null>(null);
  const [sourceId, setSourceId] = useState(""), [sourceOffset, setSourceOffset] = useState(0), [confirmed, setConfirmed] = useState(false);
  const [page, setPage] = useState<IndustryMomExperimentPage | null>(null), [offset, setOffset] = useState(0), [selected, setSelected] = useState(initialId);
  const [refresh, setRefresh] = useState(0), [error, setError] = useState(""), [sourceError, setSourceError] = useState(""), [busy, setBusy] = useState(false);
  const mounted = useRef(false), sending = useRef(false);
  const mutation = useRecoverableMutation<IndustryMomAck>(recoveryKey(workspaceScope.workspaceId, "industry-mom-create"), base);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController();
    setSources(null); setSourceError("");
    void api<IndustryMomSourcePage>(`/industry-mom-sources?limit=20&offset=${sourceOffset}`, {signal: abort.signal}).then(value => {
      if (workspaceScope.isCurrent(captured, abort.signal)) setSources(value);
    }).catch(e => { if (workspaceScope.isCurrent(captured, abort.signal)) setSourceError(describeError(e)); });
    return () => abort.abort();
  }, [sourceOffset, refresh, workspaceScope]);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController();
    setSource(null); setConfirmed(false); setSourceError("");
    if (sourceId) void api<IndustryMomSourceDetail>(`/industry-mom-sources/${encodeURIComponent(sourceId)}`, {signal: abort.signal}).then(value => {
      if (workspaceScope.isCurrent(captured, abort.signal)) setSource(value);
    }).catch(e => { if (workspaceScope.isCurrent(captured, abort.signal)) setSourceError(`来源完整核验失败；运行未就绪。${describeError(e)}`); });
    return () => abort.abort();
  }, [sourceId, refresh, workspaceScope]);
  useEffect(() => {
    const captured = workspaceScope.capture(), abort = new AbortController(); setPage(null);
    void api<IndustryMomExperimentPage>(`${base}?limit=20&offset=${offset}`, {signal: abort.signal}).then(value => {
      if (workspaceScope.isCurrent(captured, abort.signal)) { setPage(value); setSelected(old => old || value.items[0]?.id || ""); }
    }).catch(e => { if (workspaceScope.isCurrent(captured, abort.signal)) setError(describeError(e)); });
    return () => abort.abort();
  }, [offset, refresh, workspaceScope]);
  async function submit(body?: Record<string, unknown>) {
    if (sending.current) return; sending.current = true; setBusy(true); setError(""); const captured = workspaceScope.capture();
    try {
      const value = await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
      setSelected(value.experiment_id); setOffset(0); setRefresh(v => v + 1);
      mutation.acknowledge(() => setConfirmed(false));
    } catch (e) { if (mounted.current && workspaceScope.isCurrent(captured)) setError(describeError(e)); }
    finally { sending.current = false; if (mounted.current && workspaceScope.isCurrent(captured)) setBusy(false); }
  }
  const locked = busy || !mutation.ready || mutation.blocked || !!mutation.pending;
  return <div style={{overflowWrap: "anywhere"}}>
    <section className="panel" aria-label="行业组合 MOM 入口">
      <h2>行业组合 MOM · 真实历史来源</h2><p>{scopeNote}</p>
      <p className="fine-print">此任务独立于侧边栏的日度研究、旧受控月度夹具和作者个股准入。AI 未启用；计算通过不自动成为人工判断。</p>
      {error && <p role="alert">{error}</p>}{sourceError && <p role="alert">{sourceError}</p>}
      <MutationRecovery name="行业 MOM 实验" pending={mutation.pending} error={mutation.error} busy={busy} onRetry={() => void submit()} onDismiss={mutation.dismissRejected}/>
      {sources?.total === 0 && <p role="status">尚无注册的行业来源。请使用本地来源注册命令导入已完整核验的 MOM 产物，再刷新来源；此页面不使用合成数据替代，也不接受任意文件路径或 URL。</p>}
      <details open={!sources?.total}><summary>选择来源并运行固定方法</summary><fieldset disabled={locked}>
        <Field label="行业 MOM 来源"><select value={sourceId} onChange={e => setSourceId(e.target.value)}>
          <option value="">请选择已注册且可核验的来源</option>
          {source && !sources?.items.some(item => item.id === source.id) && <option value={source.id}>{source.title}</option>}
          {sources?.items.map(item => <option key={item.id} value={item.id}>{item.title} · {item.id.slice(-8)}</option>)}
        </select></Field>
        <div className="button-row"><button className="button small" disabled={!sourceOffset} onClick={() => {setSourceOffset(v => Math.max(0, v - 20)); setSourceId("");}}>上一页行业来源</button>
          <button className="button small" disabled={!sources || sourceOffset + 20 >= sources.total} onClick={() => {setSourceOffset(v => v + 20); setSourceId("");}}>下一页行业来源</button></div>
        {sourceId && !source && !sourceError && <p role="status">正在完整核验所选来源。</p>}
        {source && <><p>输入 2009-01 至 2011-12；开发 2010-01 至 2011-12。每个 H 月只复合 H−12..H−2 的十一项历史收益，跳过 H−1，缺月和空值不填补，零收益有效。最少 30 个形成资产和三分组权重均为固定项目约定。</p>
          <p>保留 2012–2013 不评价；完整原 ZIP 含后来字节。当前来源是事后修订历史，不能保证当年可得或称为完全盲测。原值 /100 为固定单位合同；有限日月核对的两项不匹配仍保留。</p>
          <JsonDetails label="检查行业来源、冻结方法与摘要" value={source}/>
          <label><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)}/>我已查看此次固定来源、方法和研究范围</label>
        </>}
        <button className="button primary" disabled={!source || !confirmed} onClick={() => source && void submit({source_id: source.id, source_digest: source.digest})}>运行行业 MOM</button>
      </fieldset></details>
      <button className="button small" disabled={busy} onClick={() => setRefresh(v => v + 1)}>刷新行业来源和实验</button>
    </section>
    <section className="panel" aria-label="行业 MOM 实验目录"><h2>行业 MOM 执行记录</h2>
      <p>{page ? `本页 ${page.items.length} / 共 ${page.total} 项` : "正在读取行业实验"}</p>
      {page?.items.map(item => <p key={item.id}><button className="text-button" aria-label={`查看行业 MOM 实验 ${item.id}`} onClick={() => setSelected(item.id)}>{formatDate(item.created_at)} · {stateName(item.status)} · {item.id}</button></p>)}
      <div className="button-row"><button className="button small" disabled={!offset} onClick={() => setOffset(v => Math.max(0, v - 20))}>上一页行业实验</button><button className="button small" disabled={!page || offset + 20 >= page.total} onClick={() => setOffset(v => v + 20)}>下一页行业实验</button></div>
    </section>
    {selected && <IndustryMomDetail key={`${workspaceScope.key}:${selected}`} id={selected} workspaceScope={workspaceScope} refresh={refresh} onChanged={() => setRefresh(v => v + 1)} onOpenCase={onOpenCase}/>}
  </div>;
}

function IndustryMomDetail({id, workspaceScope, refresh, onChanged, onOpenCase}: {
  id: string; workspaceScope: WorkspaceRequestScope; refresh: number; onChanged: () => void; onOpenCase: (id: string) => void;
}) {
  const [detail, setDetail] = useState<IndustryMomExperimentDetail | null>(null), [status, setStatus] = useState<IndustryMomStatus | null>(null);
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const mounted = useRef(false), sending = useRef(false), epoch = useRef(0), token = useRef<string | null>(null);
  const visible = useRef(detail); visible.current = detail;
  const detailAbort = useRef<AbortController | null>(null);
  const retry = useRecoverableMutation<IndustryMomAck>(recoveryKey(workspaceScope.workspaceId, "industry-mom-retry", id), `${base}/${id}/retry`);
  const cancel = useRecoverableMutation<IndustryMomAck>(recoveryKey(workspaceScope.workspaceId, "industry-mom-cancel", id), `${base}/${id}/cancel`);
  const caseMutation = useRecoverableMutation<CaseDetail>(recoveryKey(workspaceScope.workspaceId, "industry-mom-case", id), "/research-cases");
  const loadDetail = useCallback(async () => {
    detailAbort.current?.abort(); const abort = new AbortController(); detailAbort.current = abort;
    const captured = workspaceScope.capture(), sequence = ++epoch.current;
    const current = () => mounted.current && sequence === epoch.current && workspaceScope.isCurrent(captured, abort.signal);
    setLoading(true); setError("");
    try { const value = await api<IndustryMomExperimentDetail>(`${base}/${encodeURIComponent(id)}`, {signal: abort.signal}); if (current()) setDetail(value); }
    catch (e) { if (current()) {setDetail(null); setError(`完整结果核验失败；旧输出已隐藏。${describeError(e)}`);} }
    finally { if (current()) setLoading(false); }
  }, [id, workspaceScope]);
  useEffect(() => {
    mounted.current = true; const captured = workspaceScope.capture(), abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    void loadDetail();
    async function poll() {
      let delay = 5000;
      try {
        const value = await api<IndustryMomStatus>(`${base}/${encodeURIComponent(id)}/status`, {signal: abort.signal});
        if (!mounted.current || !workspaceScope.isCurrent(captured, abort.signal)) return;
        setStatus(value); delay = industryActive(value.status) ? 1500 : 5000;
        const previous = visible.current?.experiment, targetChanged = !previous || previous.attempt_id !== value.attempt_id || previous.status !== value.status;
        const changed = token.current !== value.change_token && (token.current !== null || targetChanged); token.current = value.change_token;
        if (changed) { if (targetChanged) setDetail(null); await loadDetail(); }
      } catch (e) { if (mounted.current && workspaceScope.isCurrent(captured, abort.signal)) setError(`状态同步暂未成功。${describeError(e)}`); }
      if (mounted.current && workspaceScope.isCurrent(captured, abort.signal)) timer = setTimeout(() => void poll(), delay);
    }
    timer = setTimeout(() => void poll(), 750);
    return () => { mounted.current = false; abort.abort(); clearTimeout(timer); detailAbort.current?.abort(); };
  }, [id, workspaceScope, loadDetail, refresh]);
  const output = useMemo(() => {
    try { return {result: industryVerifiedResult(detail), error: ""}; }
    catch (e) { return {result: null, error: describeError(e)}; }
  }, [detail]);
  const target = output.result ? detail?.review_target : null;
  async function action(kind: "retry" | "cancel", body?: Record<string, unknown>) {
    if (sending.current) return; sending.current = true; setBusy(true); setError(""); setNotice(""); const captured = workspaceScope.capture();
    try {
      const mutation = kind === "retry" ? retry : cancel; await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
      mutation.acknowledge(); setNotice("行业操作已保存，状态以服务端记录为准。"); await loadDetail(); onChanged();
    } catch (e) { if (mounted.current && workspaceScope.isCurrent(captured)) setError(describeError(e)); }
    finally { sending.current = false; if (mounted.current && workspaceScope.isCurrent(captured)) setBusy(false); }
  }
  async function createCase() {
    if (sending.current) return; sending.current = true; setBusy(true); setError(""); const captured = workspaceScope.capture();
    try {
      let body: Record<string, unknown> | undefined;
      if (!caseMutation.pending) {
        if (!target || !detail) throw new Error("当前尚无完整核验通过的准确行业结果。");
        const preview = await api<CasePreview>(`/research-cases/preview?${new URLSearchParams({source_kind: "industry_mom_experiment", source_id: id})}`);
        if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
        if (preview.source_kind !== "industry_mom_experiment" || preview.source_id !== id || preview.context.state !== "ready_for_review") throw new Error("案例来源与本次行业结果不匹配。");
        const exactResult = preview.context.results.filter(result => result.kind === "industry_mom_portfolio" && result.id === target.attempt_id && result.digest === target.result_digest);
        if (exactResult.length !== 1 || preview.context.results.length !== 1) throw new Error("案例预览已不属于当前显示的准确尝试；请完整核验并刷新行业结果。");
        body = caseCreateBody(preview, `行业 MOM · ${detail.source.title}`.slice(0, 200), "固定行业组合研究修改案例；经济解释仍待人工审核。");
      }
      const saved = await caseMutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(captured)) return;
      caseMutation.acknowledge(); onOpenCase(saved.id);
    } catch (e) { if (mounted.current && workspaceScope.isCurrent(captured)) setError(describeError(e)); }
    finally { sending.current = false; if (mounted.current && workspaceScope.isCurrent(captured)) setBusy(false); }
  }
  const experiment = detail?.experiment, locked = busy || loading || !!retry.pending || !!cancel.pending || !!caseMutation.pending;
  return <section className="panel" aria-label="行业 MOM 实验详情" data-industry-experiment-id={id}>
    <h2>行业 MOM 实验 {id}</h2>
    <p>流程状态：{stateName(status?.status || experiment?.status || "正在读取")} · 阶段 {status?.phase || experiment?.phase || "未记录"}。下方只显示通过完整核验的本次计算结果。</p>
    {error && <p role="alert">{error}</p>}{output.error && <p role="alert">{output.error}</p>}{notice && <p role="status">{notice}</p>}
    <MutationRecovery name="行业 MOM 重试" pending={retry.pending} error={retry.error} busy={busy} onRetry={() => void action("retry")} onDismiss={retry.dismissRejected}/>
    <MutationRecovery name="行业 MOM 取消" pending={cancel.pending} error={cancel.error} busy={busy} onRetry={() => void action("cancel")} onDismiss={cancel.dismissRejected}/>
    <MutationRecovery name="行业 MOM 案例" pending={caseMutation.pending} error={caseMutation.error} busy={busy} onRetry={() => void createCase()} onDismiss={caseMutation.dismissRejected}/>
    <div className="button-row"><button className="button" disabled={loading} onClick={() => void loadDetail()}>完整核验并刷新行业结果</button>
      {experiment && industryActive(experiment.status) && <button className="button" disabled={locked || !cancel.ready || cancel.blocked || experiment.status === "cancelling"} onClick={() => void action("cancel", {expected_attempt_id: experiment.attempt_id})}>取消行业 MOM 实验</button>}
      {experiment && industryCanRetry(experiment) && <button className="button" disabled={locked || !retry.ready || retry.blocked} onClick={() => void action("retry", {expected_attempt_id: experiment.attempt_id})}>重试行业 MOM 实验</button>}
    </div>
    {loading && <p role="status">正在完整核验行业输入、独立参考与计算输出。</p>}
    {detail && <><p>尝试 {experiment?.attempt_id || "未开始"}，共 {experiment?.attempt_count}/3 次；每次后台上限 60 秒。{experiment?.error || ""}</p>
      <p>本地字节与计算参考核验：{target ? "通过，人工评审仍待填写" : "尚无通过完整核验的准确结果"}。</p>
      <JsonDetails label="行业任务、来源和全部尝试身份" value={{experiment, source: detail.source, attempts: detail.attempts, verification: detail.verification}}/>
    </>}
    {output.result && target && <><IndustryMomResultView key={`${target.attempt_id}:${target.result_digest}`} result={output.result}/>
      <div className="button-row"><a className="button" href={industryReportPath(id, target)} download>下载本次行业技术报告</a>
        <button className="button primary" disabled={locked || !caseMutation.ready || caseMutation.blocked} onClick={() => void createCase()}>关联此结果并进入研究审核</button></div>
      <p className="fine-print">审核进入 Case → 准确结论 → 五维判断；保存案例不产生人工标签。技术报告由保存的工具结果生成；下载再次检查准确尝试和结果摘要。</p>
      <JsonDetails label="行业结果和精确审核目标" value={{target, result: output.result, reference: detail?.reference_json}}/>
    </>}
  </section>;
}

export function IndustryMomResultView({result}: {result: IndustryMomResult}) {
  const [monthId, setMonthId] = useState(result.months[0]?.month || ""); const month = result.months.find(item => item.month === monthId);
  return <section aria-label="行业 MOM 实际输出"><h3>研究计算状态：{stateName(result.status)}</h3>
    <p>{result.data_kind} · {result.asset_kind} · {result.source_id}</p><p>{scopeNote}</p>
    <p>指标直接读取保存的工具输出；毛收益指数为月度复合口径，缺月不填零、不跨缺月拼接。年化均值/波动率比值采用零无风险收益约定，不是含实际 RF 的 Sharpe。</p>
    <div className="table-scroll"><table aria-label="行业 MOM 摘要指标"><thead><tr><th>计算组合</th><th>有效 / 总月份</th><th>月均毛收益</th><th>年化样本波动率</th><th>均值/波动率比值（零 RF）</th><th>期末毛收益指数</th><th>最大毛回撤</th><th>完整路径</th></tr></thead><tbody>{result.summary.map(metric => <tr key={metric.strategy_id}>
      <th scope="row">{industryStrategyNames[metric.strategy_id]}</th><td>{metric.months_evaluated} / {metric.months_total}</td><td>{industryNumber(metric.mean_gross_return, true)}</td><td>{industryNumber(metric.annualized_sample_volatility, true)}</td><td>{industryNumber(metric.annualized_mean_over_volatility_zero_rf)}</td><td>{industryNumber(metric.terminal_gross_return_index)}</td><td>{industryNumber(metric.max_gross_drawdown, true)}</td><td>{metric.cumulative_complete ? "完整" : "不完整，不补接"}</td>
    </tr>)}</tbody></table></div>
    <p>配对可评估 {result.paired_comparison.months_evaluated} / {result.paired_comparison.months_total} 月；平均毛收益差 {industryNumber(result.paired_comparison.mean_gross_difference, true)}。双方净敞口不同，不作为优越性结论。</p>
    <Field label="查看行业 MOM 月份"><select value={monthId} onChange={e => setMonthId(e.target.value)}>{result.months.map(item => <option key={item.month} value={item.month}>{item.month} · {stateName(item.status)}</option>)}</select></Field>
    {month && <section aria-label={`行业月度明细 ${month.month}`}><h4>{month.month} · {stateName(month.status)}</h4>
      <p>形成时点 {month.as_of}；形成月份 {month.formation_months.join("、")}；跳过 {month.skipped_month}；合资格 {month.eligible_assets.length} 个行业。</p>
      <div className="table-scroll"><table><thead><tr><th>组合</th><th>月毛收益</th><th>gross / net 敞口</th><th>毛收益指数 / 回撤</th><th>不可用原因</th></tr></thead><tbody>{month.strategies.map(item => <tr key={item.id}><th>{industryStrategyNames[item.id]}</th><td>{industryNumber(item.gross_return, true)}</td><td>{industryNumber(item.gross_exposure)} / {industryNumber(item.net_exposure)}</td><td>{industryNumber(item.gross_return_index)} / {industryNumber(item.gross_drawdown, true)}</td><td>{item.reasons.join("；") || "无"}</td></tr>)}</tbody></table></div>
      <details><summary>检查本月信号、冻结权重和持有标签</summary>
        {month.strategies.map(item => <div key={item.id}><h5>{industryStrategyNames[item.id]} · 形成权重</h5><div className="table-scroll"><table><thead><tr><th>行业</th><th>权重</th></tr></thead><tbody>{item.weights.map(weight => <tr key={weight.asset}><td>{weight.asset}</td><td>{industryNumber(weight.weight, true)}</td></tr>)}</tbody></table></div></div>)}
        <div className="table-scroll"><table><thead><tr><th>行业</th><th>十一月 MOM</th><th>分组</th></tr></thead><tbody>{month.signals.map(item => <tr key={item.asset}><td>{item.asset}</td><td>{industryNumber(item.momentum, true)}</td><td>{item.mom_group}</td></tr>)}</tbody></table></div>
        <div className="table-scroll"><table><thead><tr><th>行业</th><th>持有标签</th><th>不可用原因</th></tr></thead><tbody>{month.labels.map(item => <tr key={item.asset}><td>{item.asset}</td><td>{industryNumber(item.return_value, true)}</td><td>{item.reasons.join("；") || "完整"}</td></tr>)}</tbody></table></div>
        <JsonDetails label="本月形成排除原因" value={month.exclusions}/>
      </details>
    </section>}
    <details><summary>来源、项目修改和计算限制</summary><p>当前源历史会随 CRSP 修订；不是当年可交易快照。保留期未评价，但完整原文件含后期字节；单位采用明确 /100 合同，有限一致性证据不证明日月全等。人工判断尚未产生，模型质量尚未测量。</p><ul>{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul><JsonDetails label="本次行业方法、配置与来源摘要" value={{source: result.source, config: result.config, config_digest: result.config_digest, panel_digest: result.input_digest}}/></details>
  </section>;
}
