import type {StudyDetail, StudyReview} from "./generated/api-contract";

export const MAX_AUTHOR_SCAN_BYTES = 256 * 1024;
export const studyDecisionNames: Record<StudyReview["decision"], string> = {
  data_insufficient: "数据不足", rules_unresolved: "方法规则未决", implementation_error: "实现有误", accepted_with_limits: "接受筛查结果及其限制",
};
export const studyStatusName = (status: string) => status === "screen_passed" ? "筛查完成：达到声明的数量门槛" : status === "screen_blocked" ? "筛查完成：未达到门槛" : status;
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
function finiteJson(value: unknown, depth = 0): void {
  if (depth > 100) throw new Error("扫描 JSON 嵌套过深。");
  if (typeof value === "number" && !Number.isFinite(value)) throw new Error("扫描包含无法表示的数值，不能自动改为 null。");
  if (Array.isArray(value)) value.forEach(item => finiteJson(item, depth + 1));
  else if (record(value)) Object.values(value).forEach(item => finiteJson(item, depth + 1));
}
export function studyFileError(file: Pick<File, "name" | "size"> | null): string {
  if (!file) return "请选择 CLI 生成的扫描 input.json。";
  if (!file.name.toLowerCase().endsWith(".json")) return "请选择扫描 JSON；原 MAT 通过 CLI 扫描，不上传工作台。";
  return file.size <= 0 || file.size > MAX_AUTHOR_SCAN_BYTES ? "扫描 JSON 需要非空且不超过 256 KiB。" : "";
}
/** Convenience checks only. The server owns plan, source, digest and count consistency. */
export function parseStudyScan(text: string): Record<string, unknown> {
  if (!text || new TextEncoder().encode(text).byteLength > MAX_AUTHOR_SCAN_BYTES) throw new Error("扫描 JSON 需要非空且不超过 256 KiB。");
  let value: unknown;
  try { value = JSON.parse(text); } catch { throw new Error("无法读取扫描 JSON，请选择 input.json，不要选择计划或报告。"); }
  finiteJson(value);
  if (!record(value) || value.schema_version !== 1 || value.kind !== "author_eligibility_scan" || value.semantics_version !== "gjs-author-eligibility-v1" ||
      !record(value.source) || !record(value.plan) || typeof value.plan_digest !== "string" || !Array.isArray(value.months) ||
      !value.months.length || value.months.length > 180 || !value.months.every(record)) throw new Error("文件不是受支持的作者资格扫描；完整计划和计数由服务验证。");
  return value;
}
export function studyPanelIds(text: string): string[] {
  const ids = text.split(/[\s,，]+/).filter(Boolean);
  if (ids.length > 8 || new Set(ids).size !== ids.length || ids.some(id => !/^author_panel_[0-9a-f]{64}$/.test(id))) throw new Error("关联作者面板最多 8 个、不重复；每行填写一个完整 author_panel_ 标识。服务将核验同源与月份。");
  return ids;
}
export function studyReviewBody(detail: StudyDetail | null, decision: StudyReview["decision"], note: string, actor: StudyReview["actor"]) {
  if (!detail || detail.verification_scope !== "aggregate_consistency_only" || detail.raw_source_reverified !== false || detail.result.execution_ready !== false) throw new Error("请先完整读取当前研究准入结果，再记录审核。");
  if (!Object.hasOwn(studyDecisionNames, decision) || !["human", "automation"].includes(actor) || !note.trim() || note.length > 4000) throw new Error("请选择审核结论、声明来源并填写 1–4000 字审核依据。");
  return {study_digest: detail.digest, decision, note, actor};
}
