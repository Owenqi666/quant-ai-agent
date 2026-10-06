import { activeStatuses, shortId } from "./domain";
import type { Research, Run } from "./domain";

/** Keep an explicitly selected historical item outside a bounded first page. */
export function retainSelected<T extends { id: string }>(rows: T[], selected: T | null | undefined): T[] {
  return selected && !rows.some((row) => row.id === selected.id) ? [...rows, selected] : rows;
}

export function selectedResearchRun(researchId: string, selectedId: string, run: Run | null): Run | null {
  return run?.id === selectedId && run.research_id === researchId ? run : null;
}

export function revisionLabel(research: Research, revisionId: string): string {
  const revision = research.revisions?.find((item) => item.id === revisionId);
  return revision ? `v${revision.number}` : `修订 ${shortId(revisionId)}`;
}

/** A new report request requires one deliberate, terminal, same-research choice. */
export function currentRunReport(researchId: string, run: Run | null): { run_ids: string[] } {
  if (!run || run.research_id !== researchId) throw new Error("请先选择当前研究的实验。");
  if (activeStatuses.has(run.status)) throw new Error("请等待当前实验结束后生成报告。");
  return { run_ids: [run.id] };
}
