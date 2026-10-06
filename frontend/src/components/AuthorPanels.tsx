import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { authorNumber, authorPanelFileError, authorState, parseAuthorPanel } from "../authorPanels";
import { describeError, formatDate } from "../domain";
import type { AuthorPanelDetail, AuthorPanelPage } from "../generated/api-contract";
import { recoveryKey } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import type { WorkspaceRequestScope } from "../workspaceScope";
import MutationRecovery from "./MutationRecovery";
import { Field, JsonDetails } from "./ui";

const base = "/author-panels";
export default function AuthorPanels({ workspaceScope }: { workspaceScope: WorkspaceRequestScope }) {
  const [title, setTitle] = useState("");
  const [note, setNote] = useState("");
  const [fileName, setFileName] = useState("");
  const [panel, setPanel] = useState<Record<string, unknown> | null>(null);
  const [reading, setReading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [page, setPage] = useState<AuthorPanelPage | null>(null);
  const [listError, setListError] = useState("");
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState("");
  const mounted = useRef(false), readEpoch = useRef(0), sendingRef = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const mutation = useRecoverableMutation<AuthorPanelDetail>(recoveryKey(workspaceScope.workspaceId, "author-panel-import-request"), base);
  const changed = useCallback(() => setRefresh(value => value + 1), []);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; readEpoch.current++; }; }, []);
  useEffect(() => {
    const workspace = workspaceScope.capture(), abort = new AbortController();
    setPage(null); setListError("");
    void api<AuthorPanelPage>(`${base}?limit=20&offset=${offset}`, { signal: abort.signal }).then(value => {
      if (!workspaceScope.isCurrent(workspace, abort.signal)) return;
      setPage(value); setSelected(previous => previous || value.items[0]?.id || "");
    }).catch(e => { if (workspaceScope.isCurrent(workspace, abort.signal)) setListError(describeError(e)); });
    return () => abort.abort();
  }, [workspaceScope, offset, refresh]);
  async function chooseFile(file: File | null) {
    const epoch = ++readEpoch.current, workspace = workspaceScope.capture();
    setPanel(null); setFileName(""); setError(""); setReading(false);
    const invalid = authorPanelFileError(file);
    if (invalid || !file) { setError(invalid); return; }
    setReading(true);
    try {
      const parsed = parseAuthorPanel(await file.text());
      if (!mounted.current || epoch !== readEpoch.current || !workspaceScope.isCurrent(workspace)) return;
      setPanel(parsed); setFileName(file.name);
    } catch (e) { if (mounted.current && epoch === readEpoch.current && workspaceScope.isCurrent(workspace)) setError(describeError(e)); }
    finally { if (mounted.current && epoch === readEpoch.current && workspaceScope.isCurrent(workspace)) setReading(false); }
  }
  async function submit(body?: Record<string, unknown>) {
    // The synchronous guard also covers two clicks before React renders disabled controls.
    if (sendingRef.current) return;
    sendingRef.current = true;
    const workspace = workspaceScope.capture(); setSending(true); setError(""); setNotice("");
    try {
      const saved = await mutation.execute(body);
      if (!mounted.current || !workspaceScope.isCurrent(workspace)) return;
      setSelected(saved.id); setOffset(0); changed();
      try {
        mutation.acknowledge(() => {
          setPanel(null); setFileName(""); setTitle(""); setNote("");
          if (fileInput.current) fileInput.current.value = "";
        });
      } catch (e) { setNotice(`作者面板已保存：${saved.id}；本机确认未完成，请用原请求安全重试。${describeError(e)}`); return; }
      setNotice(`作者面板已保存：${saved.id}。列表读取失败也不会重新提交。`);
    } catch (e) { if (mounted.current && workspaceScope.isCurrent(workspace)) setError(describeError(e)); }
    finally { sendingRef.current = false; if (mounted.current && workspaceScope.isCurrent(workspace)) setSending(false); }
  }
  const locked = sending || !!mutation.pending || mutation.blocked || !mutation.ready;
  return <>
    <section className="panel" aria-label="作者面板导入">
      <h2>导入作者月度面板</h2>
      <p>选择 CLI 核验 MAT 后生成的 input.json → 导入并检查时间对齐 → 查看原值与不可用原因 → 下载诊断报告。</p>
      <p className="info-note">来源为 Harvard Dataverse V2 的公开扰动 / 随机删除数据，并非未修改的原始行情。原行号标识匿名资产。此入口只诊断 MOM、作者预计算 DGW 及月收益的对齐，不执行论文组合或回测。</p>
      <p>工作台核验归一面板与独立计算结果；未在服务器重新核验原 MAT。CLI 可用原 MAT 再次复核；上传时声明的源哈希不等于服务器认证原文件。AI 未启用。</p>
      {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      <MutationRecovery name="作者面板" pending={mutation.pending} error={mutation.error} busy={sending} onRetry={() => void submit()} onDismiss={mutation.dismissRejected} />
      <fieldset disabled={locked}>
        <Field label="作者面板标题"><input maxLength={200} value={title} onChange={e => setTitle(e.target.value)} /></Field>
        <Field label="作者面板说明"><textarea maxLength={4000} value={note} onChange={e => setNote(e.target.value)} /></Field>
        <Field label="作者面板 input.json"><input ref={fileInput} type="file" accept=".json,application/json" onChange={e => void chooseFile(e.target.files?.[0] || null)} /></Field>
        <p className="fine-print">最多 768 KiB、512 个连续原行、13 个自然月。大文件 MAT 保留在 CLI 工作目录，不上传到 HTTP。</p>
        {reading && <p role="status">正在读取面板文件。</p>}
        {panel && <p role="status">已读取 {fileName}，提交后由服务完整校验。</p>}
        <button className="button primary" disabled={!panel || !title.trim() || reading} onClick={() => {
          if (panel && title.trim()) void submit({title: title.trim(), note, panel});
        }}>导入并诊断作者面板</button>
      </fieldset>
    </section>
    <section className="panel" aria-label="作者面板历史">
      <h2>作者面板历史</h2>
      {listError && <p role="alert">作者面板目录读取失败；已保存记录保持不变。{listError}</p>}
      <div className="button-row">
        <button className="button small" onClick={changed}>刷新作者面板历史</button>
        <button className="button small" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 20))}>上一页作者面板</button>
        <button className="button small" disabled={!page || offset + page.items.length >= page.total} onClick={() => setOffset(value => value + 20)}>下一页作者面板</button>
      </div>
      <p>{page ? `本页 ${page.items.length} / 共 ${page.total} 项；从第 ${offset + 1} 项开始。` : "正在读取作者面板目录。"}</p>
      {page?.total === 0 && <p>尚无作者面板。先通过 CLI 生成有界输入，再在上方导入。</p>}
      <div className="table-scroll"><table><thead><tr><th>面板</th><th>来源 / 目标月</th><th>形成可用 / 标签可用 / 原行数</th><th>创建时间</th></tr></thead><tbody>{page?.items.map(item => <tr key={item.id}>
        <td><button className="text-button" aria-label={`查看作者面板 ${item.id}`} onClick={() => setSelected(item.id)}>{item.title}</button></td>
        <td>{item.source.filename} / {item.selection.target_month}</td><td>{item.summary.formation_ready} / {item.summary.labels_available} / {item.summary.assets}</td><td>{formatDate(item.created_at)}</td>
      </tr>)}</tbody></table></div>
    </section>
    {selected && <AuthorPanelDetailView key={`${workspaceScope.key}:${selected}`} id={selected} workspaceScope={workspaceScope} />}
  </>;
}

function AuthorPanelDetailView({id, workspaceScope}: {id: string; workspaceScope: WorkspaceRequestScope}) {
  const [detail, setDetail] = useState<AuthorPanelDetail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [rowOffset, setRowOffset] = useState(0);
  useEffect(() => {
    const workspace = workspaceScope.capture(), abort = new AbortController();
    setDetail(null); setError(""); setLoading(true);
    void api<AuthorPanelDetail>(`${base}/${encodeURIComponent(id)}`, {signal: abort.signal}).then(value => {
      if (workspaceScope.isCurrent(workspace, abort.signal)) setDetail(value);
    }).catch(e => { if (workspaceScope.isCurrent(workspace, abort.signal)) setError(`完整核验失败；旧结果已隐藏。${describeError(e)}`); })
      .finally(() => { if (workspaceScope.isCurrent(workspace, abort.signal)) setLoading(false); });
    return () => abort.abort();
  }, [id, workspaceScope, refresh]);
  return <section className="panel" aria-label="作者面板详情" data-panel-id={id}>
    <h2>作者面板详情</h2>
    <button className="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>完整核验并刷新作者面板</button>
    {loading && <p role="status">正在核验作者面板与独立参考。</p>}{error && <p role="alert">{error}</p>}
    {detail && <>
      <h3>{detail.title}</h3><p>{detail.note}</p>
      <p>面板 {detail.id} · {formatDate(detail.created_at)} · 摘要 {detail.digest}</p>
      <p>核验范围：归一面板与诊断结果（{detail.verification_scope}）；原 MAT 在服务器复验：{detail.raw_source_reverified ? "是" : "否"}。独立 Fraction 参考：{detail.reference.passed ? "通过" : "未通过"}。</p>
      <a className="button" href={`/api${base}/${encodeURIComponent(id)}/markdown`} download={`author-panel-${id}.md`}>下载作者面板报告</a>
      <section aria-label="作者面板对齐结果">
        <h3>时间窗口与原始观察日期</h3>
        <p>MOM：{detail.result.windows.momentum_months.join("、")}；跳过月：{detail.result.windows.skip_month}；DGW / MV 形成月：{detail.result.windows.formation_month}；收益标签月 H：{detail.result.windows.label_month}。</p>
        <p>形成可用 {detail.result.summary.formation_ready} / {detail.result.summary.assets} 行；标签可用 {detail.result.summary.labels_available} 行；二者均可用 {detail.result.summary.ready_with_label} 行。标签可用性不改变形成资格。形成可用仅为项目诊断条件，不代表满足原论文所有样本筛选。</p>
        <div className="table-scroll"><table><thead><tr><th>自然月</th><th>源观察日期</th><th>用途</th></tr></thead><tbody>{detail.panel.months.map((month, i) => <tr key={month}><td>{month}</td><td>{detail.panel.source_observation_dates[i] ?? "源数据仅给月份，无日日期"}</td><td>{i < 11 ? "MOM 历史收益" : i === 11 ? "跳过收益；DGW / MV 形成月" : "H 月标签，独立检查"}</td></tr>)}</tbody></table></div>
        <h3>原行诊断</h3>
        <p>当前显示第 {rowOffset + 1}–{Math.min(rowOffset + 20, detail.result.rows.length)} 行诊断，共 {detail.result.rows.length} 行。</p>
        <div className="button-row"><button className="button small" disabled={!rowOffset} onClick={() => setRowOffset(value => Math.max(0, value - 20))}>上一页原行</button>
          <button className="button small" disabled={rowOffset + 20 >= detail.result.rows.length} onClick={() => setRowOffset(value => value + 20)}>下一页原行</button></div>
        <p>数值由服务计算并核验，浏览器仅格式化。零值保留；不可用不补零；预计算 DGW 不等于重新计算日频 ID。</p>
        <div className="table-scroll"><table><thead><tr><th>匿名资产 / 原行 / 国家</th><th>MOM</th><th>DGW / MV</th><th>H 月收益标签</th><th>形成 / 标签可用</th><th>缺失历史月 / 形成原因 / 标签原因</th></tr></thead><tbody>{detail.result.rows.slice(rowOffset, rowOffset + 20).map(row => <tr key={row.asset}>
          <td>{row.asset}<br />{row.source_row} / {row.country ?? "未提供"}</td><td>{authorNumber(row.momentum)}</td><td>{authorNumber(row.dgw)} / {authorNumber(row.market_cap)}</td><td>{authorNumber(row.label)}</td>
          <td>{row.formation_ready ? "是" : "否"} / {row.label_ready ? "是" : "否"}</td><td>{row.missing_momentum_months.join("、") || "无缺失历史月"}<br />形成：{row.reasons.filter(reason => !reason.startsWith("label_")).join("；") || "无"}<br />标签：{row.reasons.filter(reason => reason.startsWith("label_")).join("；") || "无"}</td>
        </tr>)}</tbody></table></div>
        <details><summary>检查逐月原 Return 与状态</summary>
          {detail.panel.rows.slice(rowOffset, rowOffset + 20).map(row => <details key={row.asset}><summary>{row.asset} · 原行 {row.source_row}</summary>
            <p>DGW：{authorNumber(row.dgw)} · {authorState(row.dgw_state)}；MV：{authorNumber(row.market_cap)} · {authorState(row.market_cap_state)}</p>
            <div className="table-scroll"><table><thead><tr><th>月份</th><th>原 Return</th><th>原值状态</th></tr></thead><tbody>{detail.panel.months.map((month, i) => <tr key={month}><td>{month}</td><td>{authorNumber(row.returns[i])}</td><td>{authorState(row.return_states[i])}</td></tr>)}</tbody></table></div>
          </details>)}
        </details>
        <ul>{detail.result.limitations.map((limitation, i) => <li key={i}>{limitation}</li>)}</ul>
      </section>
      <JsonDetails value={{source: detail.panel.source, selection: detail.panel.selection, reference: detail.reference, panel_digest: detail.result.panel_digest}} label="来源哈希、原行选择与独立核验明细" />
    </>}
  </section>;
}
