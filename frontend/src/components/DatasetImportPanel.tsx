import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, post } from "../api";
import { describeError, formatNumber, shortId } from "../domain";
import { importStatusNames, registrationPayload, RequestKeys, validateUploadFiles } from "../datasetImports";
import type { DatasetImport } from "../datasetImports";
import { Field, JsonDetails } from "./ui";

const storageKey = "paper-alpha-dataset-import";
function rememberedReceipt() {
  try { return localStorage.getItem(storageKey) || ""; } catch { return ""; }
}
export default function DatasetImportPanel({ onRegistered }: { onRegistered: (datasetId: string) => Promise<void> }) {
  const [title, setTitle] = useState("");
  const [csv, setCsv] = useState<File | null>(null);
  const [metadata, setMetadata] = useState<File | null>(null);
  const [inputVersion, setInputVersion] = useState(0);
  const [receiptId, setReceiptId] = useState(rememberedReceipt);
  const [receipt, setReceipt] = useState<DatasetImport | null>(null);
  const [working, setWorking] = useState("");
  const [error, setError] = useState("");
  const [requestKey, setRequestKey] = useState("");
  const keys = useRef(new RequestKeys());
  const generation = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; generation.current += 1; };
  }, []);
  function remember(value: DatasetImport) {
    if (!mounted.current) return;
    setReceipt(value);
    setReceiptId(value.id);
    try { localStorage.setItem(storageKey, value.id); } catch { /* Optional convenience only. */ }
  }
  async function perform(name: string, action: () => Promise<void>) {
    if (working) return;
    generation.current += 1;
    setWorking(name);
    setError("");
    try { await action(); } catch (e) { if (mounted.current) setError(describeError(e)); }
    finally { generation.current += 1; if (mounted.current) setWorking(""); }
  }
  async function reload() {
    const id = receiptId.trim();
    if (!id) return;
    await perform("query", async () => {
      const value = await api<DatasetImport>(`/dataset-imports/${encodeURIComponent(id)}`);
      remember(value);
      if (!["uploaded", "validating"].includes(value.status)) keys.current.complete("validate");
    });
  }
  useEffect(() => {
    if (!receipt || (!['validating', 'registering'].includes(receipt.status) && !['validate', 'register'].includes(working))) return;
    const abort = new AbortController();
    const startGeneration = generation.current;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await api<DatasetImport>(`/dataset-imports/${receipt!.id}`, { signal: abort.signal });
        if (!abort.signal.aborted && generation.current === startGeneration) remember(value);
      } catch (e) {
        if (!abort.signal.aborted && generation.current === startGeneration) setError(describeError(e));
      }
      if (!abort.signal.aborted) timer = setTimeout(poll, 2000);
    }
    timer = setTimeout(poll, 2000);
    return () => { abort.abort(); clearTimeout(timer); };
  }, [receipt?.id, receipt?.status, working]);

  async function upload(e: FormEvent) {
    e.preventDefault();
    await perform("upload", async () => {
      validateUploadFiles(csv, metadata);
      if (!title.trim()) throw new Error("请填写数据集标题。");
      const key = keys.current.get("upload", `${inputVersion}:${title.trim()}`);
      setRequestKey(key);
      const form = new FormData();
      form.append("title", title.trim()); form.append("file", csv!); form.append("metadata", metadata!);
      form.append("idempotency_key", key);
      const value = await api<DatasetImport>("/dataset-imports", { method: "POST", body: form });
      generation.current += 1;
      remember(value);
      keys.current.complete("upload");
    });
  }
  async function validate() {
    if (!receipt) return;
    await perform("validate", async () => {
      const key = keys.current.get("validate", receipt.id);
      setRequestKey(key);
      const value = await post<DatasetImport>(`/dataset-imports/${receipt.id}/validate`, { idempotency_key: key });
      generation.current += 1;
      remember(value);
      if (value.status !== "validating") keys.current.complete("validate");
    });
  }
  async function register() {
    if (!receipt) return;
    await perform("register", async () => {
      const fingerprint = `${receipt.id}:${receipt.latest_validation_attempt_id}:${receipt.latest_validation?.report_digest}`;
      const key = keys.current.get("register", fingerprint);
      setRequestKey(key);
      const value = await post<DatasetImport>(`/dataset-imports/${receipt.id}/register`, registrationPayload(receipt, key));
      generation.current += 1;
      remember(value);
      if (value.status === "registered" && value.registered_dataset_id) {
        keys.current.complete("register");
        await onRegistered(value.registered_dataset_id);
      }
    });
  }
  const report = receipt?.latest_validation?.report;
  let canRegister = false;
  if (receipt) { try { registrationPayload(receipt, "preview"); canRegister = true; } catch { /* Server remains authoritative. */ } }
  return <section className="panel">
    <h3>导入一个合成数据版本</h3>
    <p className="muted">CSV ≤16 MiB，配套 JSON ≤2 MiB；只支持声明完整日历与资产集合的合成 OHLCV。原始文件不自动修正，验证与登记分开进行。</p>
    <form onSubmit={upload}>
      <Field label="数据集标题"><input required value={title} disabled={!!working} onChange={(e) => setTitle(e.target.value)} /></Field>
      <Field label="行情 CSV 文件"><input type="file" accept=".csv,text/csv" disabled={!!working} onChange={(e) => { setCsv(e.target.files?.[0] || null); setInputVersion((v) => v + 1); }} /></Field>
      <Field label="数据元信息 JSON"><input type="file" accept=".json,application/json" disabled={!!working} onChange={(e) => { setMetadata(e.target.files?.[0] || null); setInputVersion((v) => v + 1); }} /></Field>
      <button className="button" disabled={!!working || !csv || !metadata || !title.trim()} type="submit">{working === "upload" ? "正在保存上传…" : "上传并保存导入收据"}</button>
    </form>
    <p className="fine-print">上传未收到响应时，保留文件并重试会复用当前请求标识。已有收据时请先查询；页面断开不代表服务端验证已取消。</p>
    {requestKey && <p className="fine-print">最近请求标识：{requestKey}</p>}
    <Field label="导入收据 ID"><input value={receiptId} disabled={!!working} onChange={(e) => setReceiptId(e.target.value)} /></Field>
    <button className="button small" disabled={!receiptId.trim() || !!working} onClick={() => void reload()}>按收据查询状态</button>
    {error && <p className="info-note warning" role="alert">{error}</p>}
    {receipt && <div>
      <h4>{receipt.title} · {importStatusNames[receipt.status] || receipt.status}</h4>
      <p>收据：{receipt.id} · 输入摘要：{shortId(receipt.input_digest)}</p>
      {receipt.error && <p className="info-note warning">{receipt.error}</p>}
      <div className="button-row">
        <button className="button" disabled={!!working || ['validating', 'registering', 'registered'].includes(receipt.status)} onClick={() => void validate()}>{working === "validate" ? "等待有界验证结果…" : "运行数据验证"}</button>
        <button className="button primary" disabled={!!working || !canRegister} onClick={() => void register()}>确认登记此数据版本</button>
        {receipt.registered_dataset_id && <button className="button" disabled={!!working} onClick={() => void perform("select", () => onRegistered(receipt.registered_dataset_id!))}>在新研究中选择此版本</button>}
      </div>
      {report && <>
        <h4>数据质量报告 · {report.status}</h4>
        <p className="fine-print">验证器 {report.validator.version} · 耗时 {formatNumber(report.duration_seconds, 2)} 秒 · 报告摘要 {shortId(receipt.latest_validation?.report_digest ?? undefined)}</p>
        <div className="table-scroll"><table><thead><tr><th>检查项</th><th>结果</th><th>规则码与定位</th></tr></thead><tbody>
          {report.checks.map((check) => <tr key={check.name}><td>{check.name}</td><td>{check.outcome === "passed" ? "通过" : check.outcome === "failed" ? "失败" : "未执行"}</td><td>{check.code} · {check.message}</td></tr>)}
        </tbody></table></div>
        {report.diagnostics && <section aria-label="输入错误定位">
          <p className="fine-print">{report.diagnostics.sample_note} {report.schema_version >= 2 ? "行号按 CSV 逻辑记录计数，表头为第 1 行；引号内换行不另计记录。" : "历史报告没有结构化行列定位。"}</p>
          {!!report.diagnostics.samples.length && <div className="table-scroll"><table><thead><tr><th>记录行 / 关联行</th><th>字段</th><th>日期 / 资产</th><th>原值样本</th><th>原因</th></tr></thead><tbody>{report.diagnostics.samples.map((sample, index) => <tr key={index}>
            <td>{sample.row ?? "无对应输入行"}{sample.related_rows.length ? ` / ${sample.related_rows.join(", ")}` : ""}</td><td>{sample.column ?? "—"}</td><td>{sample.date ?? "—"} / {sample.asset ?? "—"}</td><td>{sample.value ?? "—"}</td><td>{sample.reason}</td>
          </tr>)}</tbody></table></div>}
        </section>}
        {report.summary && <p>{report.summary.rows} 行 · {report.summary.sessions} 个日期 · {report.summary.assets} 个资产 · {report.summary.start_date} 至 {report.summary.end_date} · 版本 {report.summary.version}</p>}
        <p className="fine-print">数据合法性不代表研究切分、字段需求或预热长度合适；创建研究后仍须预检。</p>
        <JsonDetails value={report} label="完整质量报告与能力限制" />
      </>}
      <JsonDetails value={receipt} label="全部验证尝试、登记记录与导入事件" />
    </div>}
  </section>;
}
