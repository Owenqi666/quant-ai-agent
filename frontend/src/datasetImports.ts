import type { Dataset, JsonObject, Task } from "./domain";

export interface DiagnosticSample { row: number | null; column: string | null; date: string | null; asset: string | null; related_rows: number[]; value: string | null; reason: string }
export interface DatasetValidationReport {
  schema_version: number;
  validator: { version: string; digest: string };
  input_digest: string;
  status: string;
  checks: { name: string; outcome: "passed" | "failed" | "not_run"; code: string; message: string }[];
  summary: null | { rows: number; sessions: number; assets: number; start_date: string; end_date: string;
    version: string; data_kind: string; fields: string[]; dataset_sha256: string };
  limitations: string[];
  duration_seconds: number;
  diagnostics?: { mode: string; detected_error_count: number; all_errors_enumerated: boolean; samples: DiagnosticSample[]; sample_limit: number; sample_note: string; row_numbering?: string; first_error_location?: DiagnosticSample | null };
}
export interface DatasetValidationAttempt {
  id: string; number: number; status: string; started_at: string; finished_at: string | null;
  validator_digest: string; report_digest: string | null; report: DatasetValidationReport | null; error: string | null;
}
export interface DatasetImport {
  id: string; title: string; status: string; input_digest: string;
  registered_dataset_id: string | null; latest_validation_attempt_id: string | null;
  latest_validation: DatasetValidationAttempt | null;
  validation_attempts: DatasetValidationAttempt[]; registration_attempts: JsonObject[]; events: JsonObject[];
  error?: string | null;
}
export const importStatusNames: Record<string, string> = {
  uploaded: "已保存上传收据", validating: "验证中", valid: "验证通过，尚未登记",
  invalid: "数据验证失败", interrupted: "导入操作已中断", registering: "登记中", registered: "已登记",
};
export const MAX_CSV_BYTES = 16 * 1024 * 1024;
export const MAX_METADATA_BYTES = 2 * 1024 * 1024;
export function validateUploadFiles(csv: Pick<File, "name" | "size"> | null, metadata: Pick<File, "name" | "size"> | null) {
  if (!csv || !metadata) throw new Error("请选择 CSV 和配套 JSON 元信息文件。");
  if (!csv.name.toLowerCase().endsWith(".csv") || !metadata.name.toLowerCase().endsWith(".json"))
    throw new Error("数据文件需要 .csv，元信息文件需要 .json；最终内容仍由服务端验证。");
  if (csv.size <= 0 || csv.size > MAX_CSV_BYTES) throw new Error("CSV 需要非空且不超过 16 MiB。");
  if (metadata.size <= 0 || metadata.size > MAX_METADATA_BYTES) throw new Error("元信息需要非空且不超过 2 MiB。");
}
export function registrationPayload(receipt: DatasetImport, key: string) {
  const attempt = receipt.latest_validation;
  if (!["valid", "interrupted"].includes(receipt.status) || !attempt || attempt.id !== receipt.latest_validation_attempt_id ||
      attempt.status !== "valid" || !attempt.report_digest || attempt.report?.status !== "valid" ||
      attempt.report.input_digest !== receipt.input_digest || !attempt.report.checks.length ||
      attempt.report.checks.some((check) => check.outcome !== "passed")) {
    throw new Error("只能登记当前收据对应的完整成功验证；请刷新状态或重新验证。");
  }
  return { input_digest: receipt.input_digest, validation_attempt_id: attempt.id,
    report_digest: attempt.report_digest, idempotency_key: key };
}

/** A failed/uncertain HTTP call keeps the same key until its input changes. */
export class RequestKeys {
  private entries = new Map<string, { fingerprint: string; key: string }>();
  constructor(private create: () => string = () => crypto.randomUUID()) {}
  get(operation: string, fingerprint: string) {
    const previous = this.entries.get(operation);
    if (previous?.fingerprint === fingerprint) return previous.key;
    const key = this.create();
    this.entries.set(operation, { fingerprint, key });
    return key;
  }
  complete(operation: string) { this.entries.delete(operation); }
}

export function taskDataIssues(task: Task, dataset: Dataset): { errors: string[]; warnings: string[] } {
  const errors: string[] = [];
  const warnings: string[] = [];
  const calendar = dataset.metadata.calendar_dates;
  const universe = dataset.metadata.universe;
  if (!Array.isArray(calendar) || !Array.isArray(universe)) return {
    errors: ["所选版本缺少服务端确认的日历或资产集合，请刷新数据列表。"], warnings,
  };
  const splits = task.evaluation.splits;
  let previousEnd = -1;
  if (!splits || typeof splits !== "object" || Array.isArray(splits)) errors.push("请填写 train、validation、test 时间切分。");
  else for (const name of ["train", "validation", "test"]) {
    const bounds = (splits as JsonObject)[name] as JsonObject | undefined;
    const start = calendar.indexOf(bounds?.start), end = calendar.indexOf(bounds?.end);
    if (start < 0 || end < 0) errors.push(`${name} 的起止日期必须来自所选数据日历。`);
    else if (end - start < 2 || start <= previousEnd) errors.push(`${name} 至少包含 3 个交易日，且各区间必须依次排列、不重叠。`);
    previousEnd = end;
  }
  const minimum = task.evaluation.min_assets ?? 3;
  if (!Number.isInteger(minimum) || Number(minimum) < 3 || Number(minimum) > universe.length)
    errors.push(`min_assets 必须在 3 到 ${universe.length} 之间。`);
  const required = new Set(task.hypotheses.flatMap((hypothesis) => Array.isArray(hypothesis?.required_fields) ? hypothesis.required_fields : []));
  const missing = [...required].filter((field) => !dataset.fields.includes(field));
  if (missing.length) warnings.push(`所需字段缺失：${missing.join(", ")}。依赖这些字段的候选将被阻断；系统不会替换字段。`);
  return { errors, warnings };
}
