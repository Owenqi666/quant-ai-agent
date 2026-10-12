import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { describeError, shortId } from "../domain";
import type { DailySnapshotSchema } from "../generated/api-contract";
import { dailyUploadKey, fileDigest, finishDailyUpload, readDailyUpload, stageDailyUpload, validateDailyUploadFiles } from "../dailySnapshotUpload";
import type { PendingDailyUpload } from "../dailySnapshotUpload";
import { writeDurably } from "../reviewRecovery";
import { Field, JsonDetails } from "./ui";
import { useI18n } from "../i18n";

export default function DailySnapshotImportPanel({ workspaceId, onRegistered }: {
  workspaceId: string; onRegistered: (id: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const storageKey = dailyUploadKey(workspaceId);
  const [title, setTitle] = useState("");
  const [csv, setCsv] = useState<File | null>(null);
  const [contract, setContract] = useState<File | null>(null);
  const [receiptFile, setReceiptFile] = useState<File | null>(null);
  const [consent, setConsent] = useState(false);
  const [pending, setPending] = useState<PendingDailyUpload | null>(null);
  const [receipt, setReceipt] = useState<DailySnapshotSchema | null>(null);
  const [error, setError] = useState("");
  const [blocked, setBlocked] = useState(false);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    generation.current += 1;
    setTitle(""); setCsv(null); setContract(null); setReceiptFile(null);
    setConsent(false); setPending(null); setReceipt(null); setError(""); setBlocked(false); setBusy(false);
    try { const original = readDailyUpload(localStorage, storageKey); setPending(original); if (original) setTitle(original.identity.title); }
    catch (e) { setError(describeError(e)); setBlocked(true); }
    return () => { generation.current += 1; };
  }, [storageKey]);
  async function register() {
    if (busy || blocked) return;
    const originalGeneration = generation.current;
    const current = () => generation.current === originalGeneration;
    setBusy(true); setError("");
    let request: PendingDailyUpload | null = null;
    try {
      validateDailyUploadFiles(csv, contract, receiptFile);
      if (!title.trim() || !consent) throw new Error(t("请填写标题并同意对上传文件做字节校验。", "Enter a title and consent to byte verification."));
      const [csvHash, contractHash, receiptHash] = await Promise.all([fileDigest(csv!), fileDigest(contract!), fileDigest(receiptFile!)]);
      if (!current()) return;
      request = stageDailyUpload(localStorage, storageKey, { title: title.trim(), csv: csvHash, contract: contractHash, receipt: receiptHash }, crypto.randomUUID());
      setPending(request);
      const form = new FormData();
      form.append("title", request.identity.title); form.append("file", csv!); form.append("contract", contract!);
      form.append("source_receipt", receiptFile!); form.append("idempotency_key", request.key); form.append("allow_whole_byte_hash", "true");
      const value = await api<DailySnapshotSchema>("/daily-snapshots", { method: "POST", body: form });
      if (!current()) return; // Preserve the durable request for its original workspace.
      setReceipt(value);
      if (value.status !== "registered" || !value.registered_dataset_id) throw new Error(value.error || t("登记尚未完成，请用原文件确认原请求。", "Registration is incomplete. Confirm the original request with the same files."));
      await onRegistered(value.registered_dataset_id);
      if (!current()) return;
      finishDailyUpload(localStorage, storageKey, request); setPending(null);
    } catch (e) {
      if (!current()) return;
      if (request && e instanceof ApiError && !e.contractViolation && e.status >= 400 && e.status < 500 && e.status !== 409) {
        const rejected = { ...request, rejected: true };
        try { writeDurably(localStorage, storageKey, rejected); setPending(rejected); } catch { /* Original durable key remains pinned. */ }
      }
      setError(describeError(e));
    } finally { if (current()) setBusy(false); }
  }
  return <section className="panel">
    <h3>{t("登记日频开发快照", "Register a daily development snapshot")}</h3>
    <p>{t("上传三份对应文件。当前登记支持工程示例；只验证开发区间，最终区间仍保留。", "Upload three matching files. Registration currently supports engineering fixtures and validates the development period; final data remains held out.")}</p>
    <Field label={t("数据标题", "Dataset title")}><input value={title} disabled={busy || !!pending} onChange={e => setTitle(e.target.value)} /></Field>
    <Field label="CSV (≤64 MiB)"><input type="file" accept=".csv" disabled={busy} onChange={e => setCsv(e.target.files?.[0] || null)} /></Field>
    <Field label={t("数据合同 JSON（≤2 MiB）", "Data contract JSON (≤2 MiB)")}><input type="file" accept=".json" disabled={busy} onChange={e => setContract(e.target.files?.[0] || null)} /></Field>
    <Field label={t("来源收据 JSON（≤2 MiB）", "Source receipt JSON (≤2 MiB)")}><input type="file" accept=".json" disabled={busy} onChange={e => setReceiptFile(e.target.files?.[0] || null)} /></Field>
    <Field label={t("同意读取上传文件的全部字节以核对 SHA-256；这不授权解析或评估最终区间。", "Read all uploaded bytes to check SHA-256. This does not authorize final-period parsing or evaluation.")}><input type="checkbox" checked={consent} disabled={busy} onChange={e => setConsent(e.target.checked)} /></Field>
    {pending && <p role="status">{pending.rejected ? t("服务已明确拒绝原请求。", "The service explicitly rejected the original request.") : t("原请求待确认；重新选择相同文件后，可用原请求确认。", "The original request awaits confirmation. Reselect the same files to confirm it.")} <code>{shortId(pending.key)}</code></p>}
    {pending?.rejected && <button type="button" className="button" disabled={busy} onClick={() => {
      try { finishDailyUpload(localStorage, storageKey, pending); setPending(null); setError(""); } catch (e) { setError(describeError(e)); }
    }}>{t("解除已拒绝请求", "Dismiss rejected request")}</button>}
    {error && <p role="alert">{error}</p>}
    <button type="button" className="button" disabled={busy || blocked || !workspaceId || !consent || !csv || !contract || !receiptFile || !title.trim() || pending?.rejected}
      onClick={() => void register()}>{busy ? t("登记中…", "Registering…") : pending ? t("确认原登记请求", "Confirm original registration") : t("验证并登记", "Validate and register")}</button>
    {receipt && <div><p>{receipt.title} · {receipt.status} · {t("最终区间状态未知", "Final-period status unknown")}</p><JsonDetails value={receipt} label={t("查看登记与验证记录", "View registration and validation record")} /></div>}
  </section>;
}
