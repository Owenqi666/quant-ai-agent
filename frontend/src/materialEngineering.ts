import type { AcceptedMaterialDraft, AcceptedGroundedMaterialDraft } from "./generated/api-contract";

export type EngineeringDisposition = {
  path: string; original: string;
  disposition: "scientific_unresolved" | "project_configuration" | "blocked";
  rationale: string;
};
export type SourceMaterial = AcceptedMaterialDraft | AcceptedGroundedMaterialDraft;
export function isGroundedMaterial(material: SourceMaterial): material is AcceptedGroundedMaterialDraft {
  return "grounding_version" in material && material.grounding_version === 1 &&
    "support_bindings" in material && Array.isArray(material.support_bindings) &&
    "paper_statistics" in material && Array.isArray(material.paper_statistics);
}

/** Source paths and text are preserved; choosing engineering scope does not resolve science. */
export function initialEngineeringDispositions(material: SourceMaterial): EngineeringDisposition[] {
  const rules = material.unresolved_rules.map((original, index) => ({ path: `/unresolved_rules/${index}`, original }));
  material.hypotheses.forEach((hypothesis, index) => {
    hypothesis.unresolved_rules.forEach((original, rule) => rules.push({ path: `/hypotheses/${index}/unresolved_rules/${rule}`, original }));
  });
  return rules.map(rule => ({ ...rule, disposition: rule.original.startsWith("[scientific]") ? "scientific_unresolved" : "blocked",
    rationale: rule.original.startsWith("[scientific]") ? "科学问题保留未决；本次仅校验源公式，不形成经济机制或研究有效性结论。" : "" }));
}

export function engineeringDispositionsComplete(items: EngineeringDisposition[]): boolean {
  return items.every(item => item.disposition !== "blocked" && !!item.rationale.trim() &&
    (item.disposition !== "scientific_unresolved" || item.original.startsWith("[scientific]")));
}
