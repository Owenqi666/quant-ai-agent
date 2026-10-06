import { describe, expect, it } from "vitest";
import { MAX_CSV_BYTES, MAX_METADATA_BYTES, registrationPayload, RequestKeys, taskDataIssues, validateUploadFiles } from "./datasetImports";
import type { DatasetImport } from "./datasetImports";
import type { Dataset, Task } from "./domain";

const receipt = (): DatasetImport => ({
  id: "import", title: "Fixture", status: "valid", input_digest: "input-sha",
  latest_validation_attempt_id: "attempt", registered_dataset_id: null,
  validation_attempts: [], registration_attempts: [], events: [],
  latest_validation: { id: "attempt", number: 1, status: "valid", started_at: "now", finished_at: "now",
    validator_digest: "validator", report_digest: "report-sha", error: null,
    report: { schema_version: 1, validator: { version: "v1", digest: "validator" }, input_digest: "input-sha", status: "valid",
      checks: [{ name: "integrity", outcome: "passed", code: "matches", message: "ok" }], summary: null, limitations: [], duration_seconds: 1 } },
});
describe("dataset import boundaries", () => {
  it("requires both bounded files before uploading, leaving content validation to the server", () => {
    const csv = { name: "market.csv", size: MAX_CSV_BYTES }, metadata = { name: "metadata.json", size: MAX_METADATA_BYTES };
    expect(() => validateUploadFiles(csv, metadata)).not.toThrow();
    expect(() => validateUploadFiles(null, metadata)).toThrow("请选择");
    expect(() => validateUploadFiles({ ...csv, size: MAX_CSV_BYTES + 1 }, metadata)).toThrow("16 MiB");
    expect(() => validateUploadFiles(csv, { ...metadata, size: MAX_METADATA_BYTES + 1 })).toThrow("2 MiB");
    expect(() => validateUploadFiles({ ...csv, size: 0 }, metadata)).toThrow("非空");
    expect(() => validateUploadFiles({ ...csv, name: "run.py" }, metadata)).toThrow(".csv");
  });
  it("binds explicit registration to the exact validated input, attempt and report", () => {
    expect(registrationPayload(receipt(), "key")).toEqual({ input_digest: "input-sha", validation_attempt_id: "attempt", report_digest: "report-sha", idempotency_key: "key" });
    for (const status of ["uploaded", "validating", "invalid", "registered"]) {
      expect(() => registrationPayload({ ...receipt(), status }, "key")).toThrow("成功验证");
    }
    for (const mutation of [
      (value: DatasetImport) => { value.latest_validation_attempt_id = "newer"; },
      (value: DatasetImport) => { value.latest_validation!.report_digest = null; },
      (value: DatasetImport) => { value.latest_validation!.report!.input_digest = "other"; },
      (value: DatasetImport) => { value.latest_validation!.report!.checks[0].outcome = "not_run"; },
    ]) {
      const value = receipt(); mutation(value);
      expect(() => registrationPayload(value, "key")).toThrow();
    }
  });
  it("allows explicit registration recovery only when the latest validation is still complete", () => {
    const value = receipt(); value.status = "interrupted";
    expect(() => registrationPayload(value, "retry")).not.toThrow();
    value.latest_validation!.status = "interrupted";
    expect(() => registrationPayload(value, "retry")).toThrow();
  });
  it("reuses uncertain request keys but changes them for new content or an intentional new attempt", () => {
    let index = 0;
    const keys = new RequestKeys(() => `key-${++index}`);
    const first = keys.get("upload", "files-v1");
    expect(keys.get("upload", "files-v1")).toBe(first);
    expect(keys.get("upload", "files-v2")).not.toBe(first);
    const validation = keys.get("validate", "receipt");
    expect(keys.get("validate", "receipt")).toBe(validation);
    keys.complete("validate");
    expect(keys.get("validate", "receipt")).not.toBe(validation);
  });
});

describe("research data binding", () => {
  const calendar = Array.from({ length: 12 }, (_, index) => `2024-01-${String(index + 1).padStart(2, "0")}`);
  const dataset: Dataset = { id: "data", title: "Dataset", version: "version", sha256: "sha", data_kind: "synthetic", fields: ["open", "close"],
    metadata: { calendar_dates: calendar, universe: ["A", "B", "C"] } };
  const task = (): Task => ({ evidence: [], candidates: [], hypotheses: [{ id: "h", claim: "claim", attribution: "user_modification", signal_direction: "local convention", evidence_ids: [], required_fields: ["vwap"],
    economic_mechanism: "review", mechanism_attribution: "user_modification", assumptions: [] }], budget: {},
    evaluation: { min_assets: 3, splits: { train: { start: calendar[0], end: calendar[3] }, validation: { start: calendar[4], end: calendar[7] }, test: { start: calendar[8], end: calendar[11] } } } });
  it("allows declared missing-field negative cases and preserves the original draft", () => {
    const value = task(), before = JSON.stringify(value);
    const result = taskDataIssues(value, dataset);
    expect(result.errors).toEqual([]);
    expect(result.warnings.join()).toContain("vwap");
    expect(JSON.stringify(value)).toBe(before);
  });
  it("rejects dates or asset counts that do not fit a changed dataset without rewriting them", () => {
    const value = task(), before = JSON.stringify(value);
    const result = taskDataIssues(value, { ...dataset, metadata: { ...dataset.metadata, calendar_dates: ["2030-01-01"], universe: ["A", "B"] } });
    expect(result.errors).toHaveLength(4);
    expect(JSON.stringify(value)).toBe(before);
  });
  it("catches overlapping and too-short splits instead of changing their boundaries", () => {
    const value = task();
    value.evaluation.splits = { train: { start: calendar[0], end: calendar[3] }, validation: { start: calendar[3], end: calendar[7] }, test: { start: calendar[8], end: calendar[8] } };
    expect(taskDataIssues(value, dataset).errors).toHaveLength(2);
  });
});
