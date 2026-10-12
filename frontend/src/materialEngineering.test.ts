import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { PublicMaterialDetail } from "./generated/api-contract";
import { engineeringDispositionsComplete, initialEngineeringDispositions } from "./materialEngineering";
import type { SourceMaterial } from "./materialEngineering";
vi.mock("./i18n", () => ({ useI18n: () => ({ t: (zh: string) => zh }) }));
import MaterialAdoptionPanel from "./components/MaterialAdoptionPanel";

const source = { title: "Source formula", grounding_version: 1,
  evidence: [{ id: "e", page: 9, quote: "source formula", chunk_id: "chunk", normalized_start: 0, normalized_end: 14, printed_page: null }],
  hypotheses: [{ id: "h", claim: "Source formula", claim_attribution: "paper_original", evidence_ids: ["e"],
    economic_mechanism: null, mechanism_attribution: "unresolved", signal_direction: "Formula sign", calculation: "rank(close)", required_data: [],
    conditions: [], unresolved_rules: ["Choose execution timing"] }],
  unresolved_rules: ["[scientific] Economic mechanism is unknown"], limitations: ["No research approval"],
  support_bindings: [{ hypothesis_id: "h", component: "calculation", evidence_ids: ["e"], status: "direct", explanation: "Model-declared source support" }],
  paper_statistics: [{ label: "Paper IC", value: "0.987654", evidence_ids: ["e"], attribution: "paper_original" }] } as SourceMaterial;

describe("formula engineering preserves original unknowns and model support limits", () => {
  it("retains exact source text and paths; only explicit scientific prefixes default to scientific unknown", () => {
    const rows = initialEngineeringDispositions(source);
    expect(rows.map(item => item.path)).toEqual(["/unresolved_rules/0", "/hypotheses/0/unresolved_rules/0"]);
    expect(rows[0].disposition).toBe("scientific_unresolved"); expect(rows[1].disposition).toBe("blocked");
    expect(engineeringDispositionsComplete(rows)).toBe(false);
    rows[1].disposition = "project_configuration"; rows[1].rationale = "Apply the declared next-session execution settings.";
    expect(engineeringDispositionsComplete(rows)).toBe(true);
    expect(rows[1].original).toBe("Choose execution timing");
    rows[1].disposition = "scientific_unresolved";
    expect(engineeringDispositionsComplete(rows)).toBe(false);
  });
  it("shows separate engineering action, source diagnostics, model judgments and original statistics", () => {
    const material = { id: "material", paper_id: "paper", current_digest: "digest", latest_version: null, draft: source } as PublicMaterialDetail;
    const markup = renderToStaticMarkup(createElement(MaterialAdoptionPanel, { workspaceId: "test", material, datasetId: "data", busy: false,
      onAdopt: async () => true, onEngineering: async () => true }));
    expect(markup).toContain("保存公式工程配置（自动化）"); expect(markup).toContain("诊断尚未读取");
    expect(markup).toContain("pending_human"); expect(markup).toContain("支持判断来自模型");
    expect(markup).toContain("原论文统计数字（非本次计算指标）");
    expect(markup).toContain("/api/research-v2/materials/material/report?dataset_id=data");
    expect(markup).toContain("采纳选定材料");
  });
});
