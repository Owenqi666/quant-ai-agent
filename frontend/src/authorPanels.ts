/** Input convenience checks; authoritative source and scientific validation is server-owned. */
export const MAX_AUTHOR_PANEL_BYTES = 768 * 1024;
export function authorPanelFileError(file: Pick<File, "name" | "size"> | null): string {
  if (!file) return "请选择 CLI 生成的 input.json 面板文件。";
  if (!file.name.toLowerCase().endsWith(".json")) return "请选择 JSON 面板；MAT 原文件须先由 CLI 进行核验和有界提取。";
  if (file.size <= 0 || file.size > MAX_AUTHOR_PANEL_BYTES) return "面板 JSON 需要非空且不超过 768 KiB。";
  return "";
}
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
function checkFinite(value: unknown, depth = 0): void {
  if (depth > 100) throw new Error("面板 JSON 嵌套过深。");
  if (typeof value === "number" && !Number.isFinite(value)) throw new Error("面板含无法表示的数值；请保留原文件并重新运行 CLI。");
  if (Array.isArray(value)) for (const item of value) checkFinite(item, depth + 1);
  else if (record(value)) for (const item of Object.values(value)) checkFinite(item, depth + 1);
}
export function parseAuthorPanel(text: string): Record<string, unknown> {
  if (!text || new TextEncoder().encode(text).byteLength > MAX_AUTHOR_PANEL_BYTES) throw new Error("面板 JSON 需要非空且不超过 768 KiB。");
  let value: unknown;
  try { value = JSON.parse(text); }
  catch { throw new Error("无法读取面板 JSON。请选择 CLI 生成的 input.json，不要选择报告或清单。"); }
  checkFinite(value);
  if (!record(value) || value.kind !== "author_perturbed_monthly_panel" || value.schema_version !== 1 ||
      value.adapter_version !== "gjs-v2-monthly-panel-v1" || !record(value.source) || !record(value.selection) ||
      !Array.isArray(value.months) || value.months.length !== 13 || !value.months.every(item => typeof item === "string") ||
      !Array.isArray(value.rows) || value.rows.length < 1 || value.rows.length > 512 || !value.rows.every(record)) {
    throw new Error("文件不是受支持的作者月度面板；请选择 CLI 生成的 input.json。完整字段与时间对齐将由服务端校验。");
  }
  return value;
}
export function authorNumber(value: number | null): string {
  return value === null ? "不可用" : String(value);
}
export function authorState(state: string): string {
  return ({value: "原值", nan: "NaN（缺失）", posinf: "+∞（不可用）", neginf: "−∞（不可用）"} as Record<string, string>)[state] || state;
}
