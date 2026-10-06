import { ApiError } from "./api";
import type { Research } from "./domain";

export const researchSelectionKey = (workspace: string) => `paper-alpha:selection:v1:${workspace}`;
export function browserSelectionStorage(): Storage | null {
  try { return globalThis.localStorage ?? null; } catch { return null; }
}
export function savedResearchSelection(storage: Pick<Storage, "getItem"> | null, workspace: string): string {
  try {
    // The old unscoped value is a migration candidate only; callers must verify it.
    return storage?.getItem(researchSelectionKey(workspace)) ?? storage?.getItem("paper-alpha-research") ?? "";
  } catch { return ""; }
}
export function persistResearchSelection(storage: Pick<Storage, "setItem"> | null, workspace: string, id: string) {
  try { storage?.setItem(researchSelectionKey(workspace), id); } catch { /* Browser persistence is optional. */ }
}
export const isMissingResearch = (error: unknown) => error instanceof ApiError && error.status === 404 && !error.contractViolation;
export class MissingResearchSelection extends Error {}

/** A valid historical selection may be outside the first catalog page. */
export async function resolveResearchSelection(candidate: string, firstPage: Research[], lookup: (id: string) => Promise<Research>) {
  if (candidate) {
    try { return { research: await lookup(candidate), fellBack: false }; }
    catch (error) { if (!isMissingResearch(error)) throw error; }
  }
  const fallback = firstPage.find((row) => row.id !== candidate);
  return { research: fallback ? await lookup(fallback.id) : null, fellBack: !!candidate };
}
