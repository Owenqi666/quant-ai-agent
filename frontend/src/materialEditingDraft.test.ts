import { describe, expect, it } from "vitest";
import type { PublicMaterialDetail } from "./generated/api-contract";
import { applyMaterialEditingDraft, initialMaterialEdit, isMaterialEditingDraft } from "./materialEditingDraft";
const material = { id: "material", current_digest: "digest-a", latest_version: null, draft: { title: "source", evidence: [{ id: "e1", quote: "original source quote" }], hypotheses: [{ id: "h1", claim: "original", claim_attribution: "paper_original", evidence_ids: ["e1"], economic_mechanism: null, mechanism_attribution: "unresolved", unresolved_rules: ["unknown"] }], unresolved_rules: ["source rule"], limitations: ["unverified"] } } as unknown as PublicMaterialDetail;
describe("material user-edit drafts", () => {
  it("stores only changes and selections, then reapplies them to the exact server material version", () => {
    const edit = initialMaterialEdit(material); edit.hypotheses.h1 = { claim: "edited", claim_attribution: "user_modification" };
    const serialized = JSON.stringify(edit);
    expect(serialized).not.toContain("original source quote"); expect(serialized).not.toContain("source rule");
    const restored = applyMaterialEditingDraft(material, JSON.parse(serialized));
    expect(restored.hypotheses[0].claim).toBe("edited"); expect(restored.hypotheses[0].claim_attribution).toBe("user_modification");
    expect(restored.evidence).toEqual(material.draft.evidence); expect(restored.unresolved_rules).toEqual(["source rule"]);
    expect(material.draft.hypotheses[0].claim).toBe("original");
  });
  it("refuses stale versions and unknown hypothesis references without changing the new material", () => {
    const edit = initialMaterialEdit(material);
    expect(() => applyMaterialEditingDraft({ ...material, current_digest: "digest-b" }, edit)).toThrow(/另一材料版本/);
    expect(() => applyMaterialEditingDraft(material, { ...edit, selectedIds: ["missing"] })).toThrow(/不存在/);
  });
  it("attributes changed scientific rules and global unresolved rules to the user", () => {
    for (const patch of [{ signal_direction: "new direction" }, { calculation: "new calculation" }, { unresolved_rules: [] }]) {
      const edit = initialMaterialEdit(material); edit.hypotheses.h1 = patch;
      expect(applyMaterialEditingDraft(material, edit).hypotheses[0].claim_attribution).toBe("user_modification");
    }
    const global = { ...initialMaterialEdit(material), unresolvedRules: [] };
    expect(applyMaterialEditingDraft(material, global).hypotheses[0].claim_attribution).toBe("user_modification");
    const mechanism = initialMaterialEdit(material); mechanism.hypotheses.h1 = { economic_mechanism: "user explanation" };
    const applied = applyMaterialEditingDraft(material, mechanism).hypotheses[0];
    expect(applied.claim_attribution).toBe("paper_original"); expect(applied.mechanism_attribution).toBe("user_modification");
  });
  it("does not accept arbitrary output, evidence, or executable fields in an edit patch", () => {
    expect(isMaterialEditingDraft(initialMaterialEdit(material))).toBe(true);
    expect(isMaterialEditingDraft({ ...initialMaterialEdit(material), hypotheses: { h1: { expression: "code" } } })).toBe(false);
    expect(isMaterialEditingDraft({ ...initialMaterialEdit(material), hypotheses: { h1: { evidence: [] } } })).toBe(false);
  });
});
