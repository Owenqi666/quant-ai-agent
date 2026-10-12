import type { AcceptedMaterialDraft, AcceptedMaterialHypothesis, PublicMaterialDetail } from "./generated/api-contract";
import { draftKey } from "./drafts";

export interface MaterialEditingDraft {
  materialId: string; baseDigest: string; selectedIds: string[];
  hypotheses: Record<string, Partial<AcceptedMaterialHypothesis>>;
  unresolvedRules?: string[];
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const lines = (value: unknown): value is string[] => Array.isArray(value) && value.length <= 30 && value.every(item => typeof item === "string" && item.length <= 2000);
export const materialEditingKey = (workspace: string, materialId: string) => draftKey(workspace, `material-edit:${materialId}`);
export const initialMaterialEdit = (material: PublicMaterialDetail): MaterialEditingDraft => ({ materialId: material.id, baseDigest: material.current_digest,
  selectedIds: (material.latest_version?.material ?? material.draft).hypotheses.map(item => item.id), hypotheses: {} });
export function isMaterialEditingDraft(value: unknown): value is MaterialEditingDraft {
  if (!record(value) || typeof value.materialId !== "string" || typeof value.baseDigest !== "string" || !Array.isArray(value.selectedIds) || value.selectedIds.length > 10 ||
    !value.selectedIds.every(id => typeof id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(id)) || new Set(value.selectedIds).size !== value.selectedIds.length ||
    !record(value.hypotheses) || Object.keys(value.hypotheses).length > 10 || (value.unresolvedRules !== undefined && !lines(value.unresolvedRules))) return false;
  return Object.entries(value.hypotheses).every(([id, patch]) => /^[A-Za-z0-9_-]{1,64}$/.test(id) && record(patch) && Object.entries(patch).every(([field, item]) => {
    if (["claim", "economic_mechanism", "signal_direction", "calculation"].includes(field)) return (item === null && field !== "claim") || (typeof item === "string" && item.length <= 4000);
    if (["claim_attribution", "mechanism_attribution"].includes(field)) return ["paper_original", "model_conjecture", "unresolved", "user_modification"].includes(String(item));
    return field === "unresolved_rules" && lines(item);
  }));
}
export function applyMaterialEditingDraft(material: PublicMaterialDetail, edit: MaterialEditingDraft): AcceptedMaterialDraft {
  if (!isMaterialEditingDraft(edit) || edit.materialId !== material.id || edit.baseDigest !== material.current_digest) throw new Error("材料编辑草稿来自另一材料版本，未应用到当前提案。");
  const source = material.latest_version?.material ?? material.draft;
  const ids = new Set(source.hypotheses.map(item => item.id));
  if (edit.selectedIds.some(id => !ids.has(id)) || Object.keys(edit.hypotheses).some(id => !ids.has(id))) throw new Error("材料编辑草稿引用了不存在的假说，未应用。");
  const unresolved = edit.unresolvedRules ?? source.unresolved_rules;
  const globalChange = JSON.stringify(unresolved) !== JSON.stringify(source.unresolved_rules);
  return { ...source, hypotheses: source.hypotheses.map(item => {
    const patched = { ...item, ...edit.hypotheses[item.id] };
    if (globalChange || (["claim", "signal_direction", "calculation", "unresolved_rules"] as const).some(field => JSON.stringify(patched[field]) !== JSON.stringify(item[field]))) patched.claim_attribution = "user_modification";
    if (patched.economic_mechanism !== item.economic_mechanism) patched.mechanism_attribution = patched.economic_mechanism === null ? "unresolved" : "user_modification";
    return patched;
  }), unresolved_rules: unresolved };
}
