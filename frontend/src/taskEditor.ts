import { parseTask } from "./domain";
import type { Paper, Task } from "./domain";

export const attributionNames: Record<string, string> = {
  paper_original: "论文原文", user_modification: "我的修改 / 解释", model_conjecture: "模型推测（需人工核对）",
};
export const emptyTask = (): Task => ({ evidence: [], hypotheses: [], candidates: [],
  evaluation: { splits: { train: { start: "", end: "" }, validation: { start: "", end: "" }, test: { start: "", end: "" } }, min_assets: 3 },
  budget: { max_attempts_per_candidate: 2, max_candidates: 4, max_seconds: 60, max_tool_calls: 24 },
});
export const nextId = (prefix: string, rows: { id: string }[]) => {
  let n = 1;
  while (rows.some((row) => row.id === `${prefix}-${n}`)) n += 1;
  return `${prefix}-${n}`;
};
export const textLines = (value: string) => value.split("\n").map((line) => line.trim()).filter(Boolean);
export const normalizeQuote = (value: string) => value.normalize("NFKC").replace(/\s+/gu, " ").trim();
export const quoteMatches = (quote: string, text: string) => !!normalizeQuote(quote) && normalizeQuote(text).includes(normalizeQuote(quote));

/** Reject malformed advanced input before it can enter controlled form rendering. */
export function formTask(text: string): Task {
  const task = parseTask(text);
  for (const [name, rows, stringKeys, listKeys] of [
    ["evidence", task.evidence, ["id", "quote"], []],
    ["hypotheses", task.hypotheses, ["id", "claim", "attribution", "economic_mechanism", "mechanism_attribution", "signal_direction"], ["evidence_ids", "required_fields", "assumptions"]],
    ["candidates", task.candidates, ["id", "hypothesis_id", "expression", "origin"], ["changes"]],
  ] as const) {
    for (const value of rows) {
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} 包含无效对象。`);
      const row = value as unknown as Record<string, unknown>;
      for (const key of stringKeys) if (typeof row[key] !== "string") throw new Error(`${name}.${key} 必须为文本。`);
      for (const key of listKeys) if (!Array.isArray(row[key]) || !(row[key] as unknown[]).every((v) => typeof v === "string")) throw new Error(`${name}.${key} 必须为文本数组。`);
    }
  }
  return task;
}

export function taskFormIssues(task: Task, paper?: Paper | null): string[] {
  const errors: string[] = [];
  for (const [name, rows] of [["证据", task.evidence], ["假设", task.hypotheses], ["候选", task.candidates]] as const) {
    if (!rows.length || rows.length > 100) errors.push(`${name}需要 1–100 条。`);
    if (new Set(rows.map((r) => r.id)).size !== rows.length) errors.push(`${name}存在重复标识。`);
    if (rows.some((r) => !/^[A-Za-z0-9_-]{1,64}$/.test(r.id))) errors.push(`${name}标识仅可包含 1–64 个英文字母、数字、下划线或连字符。`);
  }
  for (const [i, e] of task.evidence.entries()) {
    if (!Number.isInteger(e.page) || e.page < 1 || !e.quote.trim()) errors.push(`证据 ${i + 1}：请选择页码并填写原文。`);
    else if (paper?.pages && !quoteMatches(e.quote, paper.pages.find((p) => p.page === e.page)?.text || "")) errors.push(`证据 ${i + 1}：引用未在所选页提取文本中匹配，请核对 PDF。`);
  }
  for (const [i, h] of task.hypotheses.entries()) {
    if (![h.claim, h.economic_mechanism, h.signal_direction].every((x) => x.trim())) errors.push(`假设 ${i + 1}：请填写主张、经济机制和信号方向。`);
    if (!attributionNames[h.attribution] || !attributionNames[h.mechanism_attribution]) errors.push(`假设 ${i + 1}：请选择主张及机制归属。`);
    if (!h.evidence_ids.length || h.evidence_ids.some((id) => !task.evidence.some((e) => e.id === id))) errors.push(`假设 ${i + 1}：请关联至少一条现存证据。`);
  }
  for (const [i, c] of task.candidates.entries()) {
    if (!c.expression.trim() || !task.hypotheses.some((h) => h.id === c.hypothesis_id)) errors.push(`候选 ${i + 1}：请填写表达式并关联假设。`);
    if (!attributionNames[c.origin]) errors.push(`候选 ${i + 1}：请选择实现来源。`);
    if (c.origin === "paper_original" ? c.changes.length > 0 : !c.changes.some((x) => x.trim())) errors.push(`候选 ${i + 1}：论文原式不应记录改动，修改或推测必须描述改动。`);
  }
  for (const [key, ceiling] of [["max_candidates", 20], ["max_attempts_per_candidate", 5], ["max_tool_calls", 200]] as const) {
    const n = task.budget[key];
    if (!Number.isInteger(n) || Number(n) < 1 || Number(n) > ceiling) errors.push(`${key} 需要为 1–${ceiling} 的整数。`);
  }
  const seconds = task.budget.max_seconds;
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0 || seconds > 600) errors.push("max_seconds 需要大于 0 且不超过 600。");
  return errors;
}

export interface TaskDifference { path: string; before: unknown; after: unknown }
export function taskDifferences(before: unknown, after: unknown, path = "任务"): TaskDifference[] {
  if (JSON.stringify(before) === JSON.stringify(after)) return [];
  if (before && after && typeof before === "object" && typeof after === "object" && !Array.isArray(before) && !Array.isArray(after)) {
    const a = before as Record<string, unknown>, b = after as Record<string, unknown>;
    return [...new Set([...Object.keys(a), ...Object.keys(b)])].flatMap((key) => taskDifferences(a[key], b[key], `${path}.${key}`));
  }
  if (Array.isArray(before) && Array.isArray(after) && [...before, ...after].every((row) => row && typeof row === "object" && typeof row.id === "string")) {
    const originalIds = before.map((row) => row.id), nextIds = after.map((row) => row.id);
    const order = originalIds.length === nextIds.length && originalIds.every((id) => nextIds.includes(id)) && originalIds.some((id, index) => nextIds[index] !== id)
      ? [{ path: `${path}.顺序`, before: originalIds, after: nextIds }] : [];
    return [...order, ...[...new Set([...originalIds, ...nextIds])].flatMap((id) => taskDifferences(before.find((row) => row.id === id), after.find((row) => row.id === id), `${path}[${id}]`))];
  }
  return [{ path, before, after }];
}
