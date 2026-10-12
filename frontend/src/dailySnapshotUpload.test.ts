import { describe, expect, it } from "vitest";
import { dailyUploadKey, finishDailyUpload, readDailyUpload, stageDailyUpload, validateDailyUploadFiles } from "./dailySnapshotUpload";

function storage() {
  const values = new Map<string, string>();
  return { getItem: (k: string) => values.get(k) ?? null, setItem: (k: string, v: string) => { values.set(k, v); }, removeItem: (k: string) => { values.delete(k); } };
}
const identity = { title: "Daily fixture", csv: "a".repeat(64), contract: "b".repeat(64), receipt: "c".repeat(64) };
describe("daily snapshot upload recovery", () => {
  it("reuses the original durable key after a lost response and blocks changed files", () => {
    const s = storage(), key = dailyUploadKey("workspace1");
    const original = stageDailyUpload(s, key, identity, "request1");
    expect(readDailyUpload(s, key)).toEqual(original);
    expect(stageDailyUpload(s, key, { ...identity }, "request2").key).toBe("request1");
    expect(() => stageDailyUpload(s, key, { ...identity, receipt: "d".repeat(64) }, "request3")).toThrow("原请求");
    expect(readDailyUpload(s, key)?.key).toBe("request1");
  });
  it("separates workspaces and preserves another mounted request", () => {
    const s = storage(), k = dailyUploadKey("one");
    const p = stageDailyUpload(s, k, identity, "one");
    expect(readDailyUpload(s, dailyUploadKey("two"))).toBeNull();
    s.setItem(k, JSON.stringify({ ...p, key: "other" }));
    expect(() => finishDailyUpload(s, k, p)).toThrow("没有清除");
    expect(readDailyUpload(s, k)?.key).toBe("other");
  });
  it("rejects corrupted state instead of silently starting another request", () => {
    const s = storage(), k = dailyUploadKey("one");
    s.setItem(k, JSON.stringify({ version: 1, key: "bad", identity: { title: "one" }, rejected: false }));
    expect(() => stageDailyUpload(s, k, identity, "new")).toThrow("恢复记录无效");
  });
  it("fails before dispatch when durable storage does not confirm the write", () => {
    expect(() => stageDailyUpload({ getItem: () => null, setItem: () => {} }, "key", identity, "new")).toThrow("本机保存");
  });
  it("accepts only three bounded matching file kinds", () => {
    const file = (name: string, size: number) => ({ name, size }) as File;
    expect(() => validateDailyUploadFiles(file("data.csv", 100), file("contract.json", 100), file("receipt.json", 100))).not.toThrow();
    expect(() => validateDailyUploadFiles(file("data.csv", 65 * 1024 * 1024), file("contract.json", 100), file("receipt.json", 100))).toThrow("64 MiB");
    expect(() => validateDailyUploadFiles(file("data.csv", 100), file("contract.json", 0), file("receipt.json", 100))).toThrow("非空 JSON");
    expect(() => validateDailyUploadFiles(null, file("contract.json", 100), file("receipt.json", 100))).toThrow("请选择 CSV");
  });
});
