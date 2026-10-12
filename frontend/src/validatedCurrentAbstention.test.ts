import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { PublicResearchProposalAttempt, ResearchProviderOutput, ResearchV2ProposalDetail } from "./generated/api-contract";
import CreationProposalDiagnostics from "./components/CreationProposalDiagnostics";
import { LanguageProvider } from "./i18n";
import { validatedCurrentAbstention } from "./validatedCurrentAbstention";

const response = (): ResearchProviderOutput => ({
  schema_version: 2, contract_id: "research_provider_v2", operation: "extract_material",
  proposal_id: "proposal_current", attempt_id: "attempt_current", context_digest: "context_current",
  outcome: "abstain", material: null, draft: null, decision: null,
  abstention: { reason_code: "EVIDENCE_INSUFFICIENT", reason: "The abstract contains no Alpha101 formula.",
    missing_requirements: ["Select the formula page.", "Add the author's signal-direction evidence."], evidence_ids: [] },
});
const legal = (): ResearchV2ProposalDetail => ({
  id: "proposal_current", operation: "extract_material", status: "abstained", current_attempt_id: "attempt_current",
  context_digest: "context_current", accepted_attempt_id: null, accepted_response: null, error: null,
  attempts: [{ id: "attempt_current", sequence: 1, status: "abstained", started_at: "2026-10-09T00:00:00Z",
    finished_at: "2026-10-09T00:00:01Z", error: null, receipt: null, output: response(), artifacts: [],
    validation: { valid: true, diagnostics: [], candidate_checks: [] } }],
} as unknown as ResearchV2ProposalDetail);
const render = (proposal: ResearchV2ProposalDetail) => renderToStaticMarkup(createElement(CreationProposalDiagnostics, { proposal }));
const attempt = (proposal: ResearchV2ProposalDetail): PublicResearchProposalAttempt => proposal.attempts[0];
afterEach(() => vi.unstubAllGlobals());

describe("current validated abstention authority", () => {
  it("reads a real-shaped valid stopped response with no adoptable accepted_response", () => {
    const proposal = legal();
    expect(proposal.accepted_response).toBeNull();
    expect(validatedCurrentAbstention(proposal)).toEqual(response().abstention);
  });

  it("requires the proposal and exact current attempt to both be abstained", () => {
    for (const status of ["running", "rejected", "ready"] as const) {
      const proposal = legal(); proposal.status = status;
      expect(validatedCurrentAbstention(proposal)).toBeNull();
    }
    const wrongAttempt = legal(); wrongAttempt.current_attempt_id = "another_attempt";
    expect(validatedCurrentAbstention(wrongAttempt)).toBeNull();
    const wrongState = legal(); attempt(wrongState).status = "rejected";
    expect(validatedCurrentAbstention(wrongState)).toBeNull();
  });

  it("requires positive validation and actual abstention output", () => {
    for (const validation of [null, { valid: false, diagnostics: [], candidate_checks: [] }]) {
      const proposal = legal(); attempt(proposal).validation = validation;
      expect(validatedCurrentAbstention(proposal)).toBeNull();
    }
    const wrongOutcome = legal(); attempt(wrongOutcome).output = { ...response(), outcome: "draft", abstention: null };
    expect(validatedCurrentAbstention(wrongOutcome)).toBeNull();
    const missingOutput = legal(); attempt(missingOutput).output = null;
    expect(validatedCurrentAbstention(missingOutput)).toBeNull();
    const missingReason = legal(); attempt(missingReason).output = { ...response(), abstention: null };
    expect(validatedCurrentAbstention(missingReason)).toBeNull();
  });

  it("rejects ambiguous duplicate current attempts and inconsistent recorded errors", () => {
    const duplicate = legal(); duplicate.attempts.push({ ...attempt(duplicate) });
    expect(validatedCurrentAbstention(duplicate)).toBeNull();
    const attemptError = legal(); attempt(attemptError).error = { code: "DOMAIN_INVALID", message: "Invalid response", retryable: false };
    expect(validatedCurrentAbstention(attemptError)).toBeNull();
    const proposalError = legal(); proposalError.error = { code: "SOURCE_CHANGED", message: "Source changed", retryable: false };
    expect(validatedCurrentAbstention(proposalError)).toBeNull();
  });

  it("does not display another proposal, attempt, context, or operation's output", () => {
    for (const override of [{ proposal_id: "foreign" }, { attempt_id: "foreign" },
      { context_digest: "foreign" }, { operation: "generate_candidates" as const }]) {
      const proposal = legal(); attempt(proposal).output = { ...response(), ...override };
      expect(validatedCurrentAbstention(proposal)).toBeNull();
    }
  });
});

describe("read-only validated stop display", () => {
  it("shows the reason code, complete reason and missing requirements directly with no dispatch", () => {
    const fetch = vi.fn(() => { throw new Error("Display must remain read-only"); });
    vi.stubGlobal("fetch", fetch);
    const proposal = legal(); const before = JSON.stringify(proposal); const output = render(proposal);
    expect(output).toContain("停止原因与待补资料");
    expect(output).toContain("本次提案已停止。");
    expect(output).toContain("EVIDENCE_INSUFFICIENT");
    expect(output).toContain("The abstract contains no Alpha101 formula.");
    expect(output).toContain("Select the formula page.");
    expect(output).toContain("Add the author&#x27;s signal-direction evidence.");
    expect(output).toContain("不会自动重试");
    expect(output).not.toContain("<button");
    expect(output).not.toContain("<details");
    expect(output).not.toContain("该响应未通过校验");
    expect(render(proposal)).toBe(output);
    expect(JSON.stringify(proposal)).toBe(before);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("renders interface labels in English while preserving the original source reason", () => {
    vi.stubGlobal("localStorage", { getItem: () => "en" });
    const output = renderToStaticMarkup(createElement(LanguageProvider, null,
      createElement(CreationProposalDiagnostics, { proposal: legal() })));
    expect(output).toContain("Stop reason and missing requirements");
    expect(output).toContain("This proposal has stopped.");
    expect(output).toContain("Missing requirements");
    expect(output).toContain("The abstract contains no Alpha101 formula.");
    expect(output).toContain("will not retry automatically");
  });

  it("keeps rejected raw abstention as an unaccepted response and never a validated stop", () => {
    const proposal = legal(); proposal.status = "rejected"; attempt(proposal).status = "rejected";
    attempt(proposal).error = { code: "EVIDENCE_NOT_EXPOSED", message: "Evidence ID was not exposed", retryable: false };
    attempt(proposal).validation = { valid: false, diagnostics: [{ code: "EVIDENCE_NOT_EXPOSED",
      message: "Evidence ID was not exposed", semantic_fidelity: "unverified" }], candidate_checks: [] };
    const output = render(proposal);
    expect(output).toContain("此响应尚未被采纳");
    expect(output).toContain("EVIDENCE_NOT_EXPOSED");
    expect(output).not.toContain("停止原因与待补资料");
    expect(output).not.toContain("本次提案已停止。");
  });

  it("ignores a previous validated abstention when the current attempt differs", () => {
    const proposal = legal(); proposal.current_attempt_id = "attempt_new";
    expect(render(proposal)).toBe("");
  });

  it("escapes source text without changing the saved response", () => {
    const proposal = legal(); const original = response();
    original.abstention!.reason = '<script>alert("source")</script>';
    original.abstention!.missing_requirements = ["<img src=x>"];
    attempt(proposal).output = original;
    const output = render(proposal);
    expect(output).toContain("&lt;script&gt;");
    expect(output).toContain("&lt;img src=x&gt;");
    expect(output).not.toContain("<script>");
    expect(attempt(proposal).output?.abstention?.reason).toBe('<script>alert("source")</script>');
  });

  it("retains an older rejected attempt alongside a current valid stop", () => {
    const proposal = legal(); proposal.attempts.unshift({ ...attempt(proposal), id: "attempt_old", sequence: 0,
      status: "rejected", error: { code: "DOMAIN_INVALID", message: "Prior rejected output", retryable: false }, output: null });
    const output = render(proposal);
    expect(output).toContain("本次提案已停止。");
    expect(output).toContain("Prior rejected output");
  });
});
