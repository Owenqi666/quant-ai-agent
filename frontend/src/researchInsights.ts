import type { Run } from "./domain";

export const reportRunLimit = 20;

/** Report selection is explicit and remains restricted to the active research. */
export function addReportRun(selected: Run[], run: Run, researchId: string): Run[] {
  if (run.research_id !== researchId) throw new Error("研究总结只能选择当前研究的实验。");
  if (selected.some((item) => item.id === run.id)) return selected;
  if (selected.length >= reportRunLimit) throw new Error(`每份总结最多选择 ${reportRunLimit} 项实验；请先移除一项。`);
  return [...selected, run];
}

/** A to B to A selection changes must not revive the first A response. */
export class InsightRequestEpoch {
  private generation = 0;
  begin() { return ++this.generation; }
  invalidate() { this.generation += 1; }
  isCurrent(token: number) { return token === this.generation; }
}

export function insightValue(value: unknown): string {
  if (value === null || value === undefined) return "未记录";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "未记录";
  if (typeof value === "string") return value || "未记录";
  return JSON.stringify(value);
}

/** UI never subtracts metrics or substitutes zero for missing values. */
export function metricDelta(value: number | null | undefined, comparable: boolean): string {
  if (!comparable || typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value > 0 ? `+${value}` : String(value);
}
