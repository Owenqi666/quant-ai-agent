import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import MaterialAdoptionPanel from "./components/MaterialAdoptionPanel";
import type { PublicMaterialDetail } from "./generated/api-contract";

const quote = "abs(x), log(x), sign(x) = standard definitions";
const delta = "delta(x, d) = today’s value of x minus the value of x d days ago";
const base = { title: "Source formula", evidence: [{ id: "operators", page: 15, chunk_id: "source-chunk", quote, normalized_start: 0, normalized_end: quote.length, printed_page: null }], hypotheses: [{ id: "h", claim: "Named source formula", claim_attribution: "paper_original", evidence_ids: ["operators"], economic_mechanism: null, mechanism_attribution: "unresolved", signal_direction: null, required_data: [], calculation: "sign(delta(close, 1))", conditions: [], unresolved_rules: ["[scientific] Mechanism unknown"] }], unresolved_rules: [], limitations: ["Unverified"] };
const original = { id: "material", paper_id: "paper", current_digest: "original", draft: base, latest_version: null } as unknown as PublicMaterialDetail;
function render(material: PublicMaterialDetail) {
  return renderToStaticMarkup(createElement(MaterialAdoptionPanel, { workspaceId: "workspace", material, busy: false, onAdopt: async () => true }));
}
describe("version-specific material citations", () => {
  it("renders newly bound definition evidence from the saved automation version while retaining the original source", () => {
    const repaired = { ...base, evidence: [...base.evidence, { id: "delta", page: 15, chunk_id: "source-chunk", quote: delta, normalized_start: 100, normalized_end: 100 + delta.length, printed_page: null }], hypotheses: [{ ...base.hypotheses[0], evidence_ids: ["operators", "delta"] }] };
    const material = { ...original, current_digest: "repaired", latest_version: { id: "version", version: 1, digest: "repaired", actor: "automation", material: repaired, definition_repair: { source_proposal_id: "saved-proposal", before_material_digest: "original", after_material_digest: "repaired", human_approval: false } } } as unknown as PublicMaterialDetail;
    const html = render(material);
    expect(html).toContain(`<blockquote>${delta}</blockquote>`);
    expect(html).toContain("自动引用修复记录"); expect(html).toContain("saved-proposal");
    expect(html).toContain("actor=automation"); expect(html).toContain("human_approval=false"); expect(html).toContain("pending_human");
    expect(original.draft.evidence).toHaveLength(1);
  });
  it("labels a zero-change automatic check without claiming citations were added", () => {
    const material = { ...original, latest_version: { id: "checked", version: 1, digest: "original", actor: "automation", material: base, definition_repair: { before_material_digest: "original", after_material_digest: "original", added_span_ids: [], human_approval: false } } } as unknown as PublicMaterialDetail;
    const html = render(material);
    expect(html).toContain("自动引用核对记录"); expect(html).toContain("本版本未修改材料");
    expect(html).not.toContain("已从本次所选原文补充定义引用"); expect(html).toContain("pending_human");
  });
  it("reads an old draft without implying that an automated repair or human approval occurred", () => {
    const html = render(original);
    expect(html).toContain(`<blockquote>${quote}</blockquote>`); expect(html).not.toContain(`<blockquote>${delta}</blockquote>`);
    expect(html).not.toContain("自动引用修复记录"); expect(original.latest_version).toBeNull();
  });
});
