import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import { describeError, formatDate } from "../domain";
import type { JsonObject } from "../domain";
import { catalogPath, emptyCatalogFilters, resourceNames, sourceNames } from "../feedback";
import type { CatalogFilters, CatalogPage, CatalogResource } from "../feedback";
import { Field, JsonDetails } from "./ui";

export default function CatalogPanel({ onOpen }: { onOpen: (resource: CatalogResource, row: JsonObject) => void }) {
  const [resource, setResource] = useState<CatalogResource>("runs");
  const [filters, setFilters] = useState<CatalogFilters>(emptyCatalogFilters);
  const [request, setRequest] = useState({ resource: "runs" as CatalogResource, filters: emptyCatalogFilters(), after: 0, through: undefined as number | undefined, offset: 0, nonce: 0 });
  const [result, setResult] = useState<{ resource: CatalogResource; page: CatalogPage<JsonObject>; offset: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError("");
    api<CatalogPage<JsonObject>>(catalogPath(request.resource, request.filters, request.after, request.through), { signal: abort.signal })
      .then((page) => { if (!abort.signal.aborted) setResult({ resource: request.resource, page, offset: request.offset }); })
      .catch((e) => { if (!abort.signal.aborted) setError(describeError(e)); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [request]);
  function search(e?: FormEvent) {
    e?.preventDefault(); setResult(null);
    setRequest({ resource, filters: { ...filters }, after: 0, through: undefined, offset: 0, nonce: request.nonce + 1 });
  }
  function next() {
    if (!result || !result.page.has_more || loading) return;
    setRequest({ ...request, after: result.page.next_cursor, through: result.page.high_watermark, offset: result.offset + result.page.items.length });
  }
  return <section className="panel">
    <h2>分页查看完整记录</h2>
    <p className="muted">每页 50 条，按服务端记录顺序读取；追加边界固定，状态可能随后变化。更改筛选或需要最新状态时，从首页重新查询。</p>
    <form onSubmit={search}>
      <Field label="记录类型"><select value={resource} onChange={(e) => setResource(e.target.value as CatalogResource)}>{Object.entries(resourceNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></Field>
      <div className="form-columns">
        {([['research_id', '筛选研究 ID'], ['dataset_id', '筛选数据版本 ID'], ['candidate_id', '筛选候选 ID']] as const).map(([key, label]) => <Field key={key} label={label}><input value={filters[key]} onChange={(e) => setFilters({ ...filters, [key]: e.target.value })} /></Field>)}
      </div>
      {["runs", "issues", "regression-checks"].includes(resource) && <Field label="筛选状态"><input value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })} placeholder="例如 failed / not_comparable / open" /></Field>}
      {["reviews", "issues"].includes(resource) && <div className="form-columns">
        <Field label="筛选错误类别"><select value={filters.category} onChange={(e) => setFilters({ ...filters, category: e.target.value })}><option value="">全部</option>{['evidence', 'hypothesis', 'implementation', 'data', 'evaluation', 'other'].map((value) => <option key={value} value={value}>{value}</option>)}</select></Field>
        <Field label="筛选审核来源"><select value={filters.source} onChange={(e) => setFilters({ ...filters, source: e.target.value })}><option value="">全部</option>{Object.entries(sourceNames).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></Field>
      </div>}
      <button type="submit" className="button primary" disabled={loading}>按条件重新查询</button>
    </form>
    {loading && <p role="status">正在读取目录页…</p>}
    {error && <div className="info-note warning"><p role="alert">{error}</p><button className="button small" disabled={loading} onClick={() => setRequest({ ...request, nonce: request.nonce + 1 })}>重试当前目录页</button></div>}
    {result && <>
      <p>{resourceNames[result.resource]} · 第 {result.page.items.length ? result.offset + 1 : 0}–{result.offset + result.page.items.length} 条 / {result.page.total_records} 条 · 追加水位 {result.page.high_watermark}</p>
      <JsonDetails value={result.page.filters} label="当前结果的实际筛选条件" />
      <div className="table-scroll"><table><thead><tr><th>记录 / 时间</th><th>状态 / 候选 / 来源</th><th>详情与操作</th></tr></thead><tbody>{result.page.items.map((row) => <tr key={String(row.id)}>
        <td>{String(row.title || row.id)}<small className="cell-subtitle">{formatDate(String(row.created_at || ""))} · {String(row.id)}</small></td>
        <td>{String(row.state || row.status || row.outcome || row.verdict || row.expected_status || "—")} / {String(row.candidate_id || "—")} / {row.source ? sourceNames[String(row.source)] || String(row.source) : "—"}</td>
        <td><JsonDetails value={row} label="完整记录与关联 ID" />{['researches', 'runs', 'issues', 'regression-cases'].includes(result.resource) && <button className="button small" onClick={() => onOpen(result.resource, row)}>{result.resource === 'regression-cases' ? "在回归窗口中使用" : "打开此记录"}</button>}</td>
      </tr>)}</tbody></table></div>
      {!result.page.items.length && <p>当前条件下暂无记录。</p>}
      <div className="button-row"><button className="button small" disabled={loading || !result.page.has_more} onClick={next}>下一页目录</button><button className="button small" disabled={loading} onClick={() => search()}>从首页读取最新记录</button></div>
    </>}
  </section>;
}
