import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { PublicResearchProposalAttempt, ResearchV2ProposalDetail } from "./generated/api-contract";
import CreationProposalDiagnostics from "./components/CreationProposalDiagnostics";
import { GenerationSourceNotice } from "./components/ResearchCreationAI";

function proposal(overrides: Partial<PublicResearchProposalAttempt> = {}): ResearchV2ProposalDetail {
  const attempt: PublicResearchProposalAttempt = {
    id: "attempt", sequence: 1, status: "rejected", started_at: "2026-10-09T00:00:00Z", finished_at: "2026-10-09T00:00:01Z",
    error: { code: "CANDIDATES_BLOCKED", message: "Provider response failed domain validation.", retryable: false },
    receipt: null, output: null, artifacts: [],
    validation: { valid: false, diagnostics: [], candidate_checks: [] }, ...overrides,
  };
  return { status: "rejected", accepted_attempt_id: null, attempts: [attempt] } as ResearchV2ProposalDetail;
}
const render = (value: ResearchV2ProposalDetail) => renderToStaticMarkup(createElement(CreationProposalDiagnostics, { proposal: value }));
afterEach(() => vi.unstubAllGlobals());

describe("AI creation read-only failure diagnostics", () => {
  it("shows attribution and candidate failures directly, without an automatic retry or a network write", () => {
    const fetch = vi.fn(() => { throw new Error("Read-only rendering must not dispatch a request"); });
    vi.stubGlobal("fetch", fetch);
    const value = proposal({ validation: { valid: false, diagnostics: [{ code: "DOMAIN_INVALID",
      message: "A changed implementation must not be labelled paper_original", semantic_fidelity: "unverified" }],
      candidate_checks: [{ candidate_id: "alpha101", status: "blocked", code: "ATTRIBUTION_INVALID", message: "origin=paper_original requires changes=[]",
        submitted_expression: "close - open", accepted_expression: null, normalization: "excluded" }] } });
    const output = render(value);
    expect(output).toContain("DOMAIN_INVALID");
    expect(output).toContain("A changed implementation must not be labelled paper_original");
    expect(output).toContain("origin=paper_original requires changes=[]");
    expect(output).toContain("close - open");
    expect(output).toContain("不会自动重试");
    expect(output).toContain("不消耗 Provider 调用额度");
    expect(output).not.toContain("<details");
    expect(output).not.toContain("<button");
    expect(render(value)).toBe(output);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("retains an unaccepted abstention and its exact evidence failure without presenting it as validated", () => {
    const value = proposal({ output: { outcome: "abstain", abstention: { reason: "Required price-unit context is absent." } } as PublicResearchProposalAttempt["output"],
      validation: { valid: false, diagnostics: [{ code: "EVIDENCE_NOT_EXPOSED", message: "Abstention cites evidence not sent in this request.", semantic_fidelity: "unverified" }], candidate_checks: [] } });
    const output = render(value);
    expect(output).toContain("EVIDENCE_NOT_EXPOSED");
    expect(output).toContain("Abstention cites evidence not sent in this request.");
    expect(output).toContain("Required price-unit context is absent.");
    expect(output).toContain("此响应尚未被采纳");
    expect(output).toContain("该响应未通过校验");
    expect(output).not.toContain("已停止此候选：依据或规则不足");
  });

  it("shows source_data field errors verbatim as escaped text, including nested field locations", () => {
    const output = render(proposal({ validation: { valid: false, candidate_checks: [], diagnostics: [{ code: "DOMAIN_INVALID",
      message: 'draft.source_data: field required; received <script>alert("fixture")</script>', semantic_fidelity: "unverified" }] } }));
    expect(output).toContain("draft.source_data: field required");
    expect(output).toContain("&lt;script&gt;");
    expect(output).not.toContain("<script>");
  });

  it("keeps absent diagnostics explicit and adds no failure claim to a clean accepted proposal", () => {
    expect(render({ ...proposal(), status: "exhausted", attempts: [] })).toContain("没有更详细的校验诊断");
    expect(render({ ...proposal(), status: "ready", attempts: [{ ...proposal().attempts[0], status: "ready", error: null,
      validation: { valid: true, diagnostics: [], candidate_checks: [] } }] })).toBe("");
  });
});


describe("AI candidate configuration recovery notice", () => {
  const notice = (configurationConfirmed: boolean, generationCurrent: boolean, hasGeneration = true) =>
    renderToStaticMarkup(createElement(GenerationSourceNotice, { hasGeneration, configurationConfirmed, generationCurrent }));
  it("asks only for reconfirmation after refresh, never claiming stale sources or asking for another paid generation", () => {
    for (const generationCurrent of [false, true]) {
      const output = notice(false, generationCurrent);
      expect(output).toContain("重新核对并确认当前配置");
      expect(output).toContain("重新确认不调用模型");
      expect(output).not.toContain("配置已变化");
      expect(output).not.toContain("请显式生成");
      expect(output).not.toContain('role="alert"');
    }
  });
  it("warns about changed sources only after configuration confirmation and a remaining identity mismatch", () => {
    expect(notice(true, false)).toContain("材料、数据或配置已变化");
    expect(notice(true, true)).toBe("");
    expect(notice(false, false, false)).toBe("");
  });
});
