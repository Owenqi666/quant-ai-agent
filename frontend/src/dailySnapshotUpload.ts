import { removeDurably, writeDurably } from "./reviewRecovery";

export interface DailyUploadIdentity { title: string; csv: string; contract: string; receipt: string }
export interface PendingDailyUpload { version: 1; identity: DailyUploadIdentity; key: string; rejected: boolean }
const hash = /^[a-f0-9]{64}$/;

export function dailyUploadKey(workspace: string): string {
  return `paper-alpha:daily-upload:v1:${JSON.stringify(workspace)}`;
}
export function readDailyUpload(storage: Pick<Storage, "getItem">, key: string): PendingDailyUpload | null {
  const raw = storage.getItem(key);
  if (!raw) return null;
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== "object") throw new Error("日频登记恢复记录无效，请保留浏览器记录并检查。");
  const p = value as Partial<PendingDailyUpload>;
  if (p.version !== 1 || typeof p.key !== "string" || !p.key.trim() || typeof p.rejected !== "boolean" ||
      !p.identity || typeof p.identity.title !== "string" || !p.identity.title.trim() ||
      ![p.identity.csv, p.identity.contract, p.identity.receipt].every(v => typeof v === "string" && hash.test(v))) {
    throw new Error("日频登记恢复记录无效，请保留浏览器记录并检查。");
  }
  return p as PendingDailyUpload;
}
export function sameDailyUpload(a: DailyUploadIdentity, b: DailyUploadIdentity): boolean {
  return a.title === b.title && a.csv === b.csv && a.contract === b.contract && a.receipt === b.receipt;
}
export function stageDailyUpload(storage: Pick<Storage, "getItem" | "setItem">, key: string,
  identity: DailyUploadIdentity, requestKey: string): PendingDailyUpload {
  const previous = readDailyUpload(storage, key);
  if (previous) {
    if (!sameDailyUpload(previous.identity, identity)) throw new Error("已有待确认登记，请重新选择原来的三份文件；不能用新内容覆盖原请求。");
    if (previous.rejected) throw new Error("原请求已明确拒绝，请先解除该请求后修改输入。");
    return previous;
  }
  const next: PendingDailyUpload = { version: 1, identity, key: requestKey, rejected: false };
  writeDurably(storage, key, next);
  return next;
}
export function finishDailyUpload(storage: Pick<Storage, "getItem" | "removeItem">, key: string, original: PendingDailyUpload): void {
  const current = readDailyUpload(storage, key);
  if (current && (current.key !== original.key || !sameDailyUpload(current.identity, original.identity))) {
    throw new Error("登记恢复记录已改变，没有清除其他请求。");
  }
  removeDurably(storage, key);
}
export function validateDailyUploadFiles(csv: File | null, contract: File | null, receipt: File | null): void {
  if (!csv || !contract || !receipt) throw new Error("请选择 CSV、数据合同 JSON 和来源收据 JSON。");
  if (csv.size <= 0 || csv.size > 64 * 1024 * 1024 || !csv.name.toLowerCase().endsWith(".csv")) throw new Error("CSV 必须为非空 .csv 文件且不超过 64 MiB。");
  for (const file of [contract, receipt]) {
    if (file.size <= 0 || file.size > 2 * 1024 * 1024 || !file.name.toLowerCase().endsWith(".json")) throw new Error("合同与收据必须为非空 JSON 文件，各不超过 2 MiB。");
  }
}
export async function fileDigest(file: File): Promise<string> {
  const raw = await file.arrayBuffer();
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", raw)), n => n.toString(16).padStart(2, "0")).join("");
}
