import { describe, expect, it } from "vitest";
import { applyEngineeringConfiguration, creationFlowMutationKey, emptyCreationFlow, isResearchCreationFlow, persistCreationFlow, proposalExecutionKind, readCreationFlowMutation, selectMaterialHypotheses } from "./researchCreationFlow";
import { draftKey } from "./drafts";
import { stageMutation } from "./reviewRecovery";
import { discoverCreationDrafts, discardCreationDraft } from "./creationDrafts";
import type { AcceptedMaterialDraft, AcceptedGroundedMaterialDraft, PublicMaterialEngineering, ResearchV2ProposalDetail } from "./generated/api-contract";
function storage() { const data = new Map<string, string>(); return { get length() { return data.size; }, key: (index: number) => [...data.keys()][index] ?? null, getItem: (key: string) => data.get(key) ?? null, setItem: (key: string, value: string) => { data.set(key, value); }, removeItem: (key: string) => { data.delete(key); } }; }
const key = draftKey("workspace", "create:blank");
function draft(store: ReturnType<typeof storage>) { store.setItem(key, JSON.stringify({ version: 1, scope: key, savedAt: "2026-10-09T10:00:00Z", value: { title: "Preserve title", paperId: "paper", datasetId: "data", taskText: "{unchanged}", note: "", creationStage: "evidence" } })); }
describe("AI creation durable references and source boundaries", () => {
  it("preserves unsaved task fields and stores only the validated flow references", () => {
    const store = storage(); draft(store); const next = { ...emptyCreationFlow(), materialId: "material" };
    persistCreationFlow(store, key, next, emptyCreationFlow());
    const result = JSON.parse(store.getItem(key)!);
    expect(result.value.title).toBe("Preserve title"); expect(result.value.taskText).toBe("{unchanged}"); expect(result.value.creationStage).toBe("evidence"); expect(result.value.creationFlow).toEqual(next);
    expect(() => persistCreationFlow(store, key, emptyCreationFlow(), emptyCreationFlow())).toThrow(/另一窗口/);
    expect(() => persistCreationFlow(store, "missing", next)).toThrow(/未持久/);
  });
  it("rejects malformed page selections and reference shapes without treating stored model output as authoritative", () => {
    expect(isResearchCreationFlow(emptyCreationFlow())).toBe(true);
    expect(isResearchCreationFlow({ ...emptyCreationFlow(), selectedPages: [15, 4] })).toBe(false);
    expect(isResearchCreationFlow({ ...emptyCreationFlow(), extraction: { sessionId: "session" } })).toBe(false);
  });
  it("an uncertain AI write pins its own draft while other drafts remain independently discardable", () => {
    const store = storage(); draft(store);
    stageMutation(store, creationFlowMutationKey("workspace", key), "/research-v2/sessions", { source: "frozen" }, "unchanged-key", { action: "session_extract", inputKey: "frozen-input" });
    expect(readCreationFlowMutation(store, creationFlowMutationKey("workspace", key))?.body.idempotency_key).toBe("unchanged-key");
    const discovery = discoverCreationDrafts(store, "workspace");
    expect(discovery.recoveryBlocked).toBe(false); expect(discovery.entries[0].aiPending).not.toBeNull();
    expect(() => discardCreationDraft(store, "workspace", discovery.entries[0])).toThrow(/AI 请求/);
    const other = key + "-other", otherValue = JSON.parse(store.getItem(key)!); otherValue.scope = other; store.setItem(other, JSON.stringify(otherValue));
    discardCreationDraft(store, "workspace", discoverCreationDrafts(store, "workspace").entries.find(entry => entry.key === other)!);
    expect(store.getItem(other)).toBeNull(); expect(store.getItem(key)).not.toBeNull();
  });
  it("a corrupt AI receipt preserves the draft without blocking unrelated draft discovery", () => {
    const store = storage(); draft(store); store.setItem(creationFlowMutationKey("workspace", key), "{bad");
    const discovered = discoverCreationDrafts(store, "workspace"); expect(discovered.recoveryBlocked).toBe(false); expect(discovered.entries[0].aiRecoveryError).not.toBeNull();
    expect(() => discardCreationDraft(store, "workspace", discovered.entries[0])).toThrow(); expect(store.getItem(key)).not.toBeNull();
  });
  it("selecting a hypothesis retains every linked evidence and all unresolved material limitations", () => {
    const material = { title: "Original", evidence: [{ id: "e1" }, { id: "e2" }, { id: "e3" }], hypotheses: [{ id: "h1", evidence_ids: ["e1"], required_data: [{ evidence_ids: ["e2"] }] }, { id: "h2", evidence_ids: ["e3"], required_data: [] }], unresolved_rules: ["unknown"], limitations: ["synthetic"] } as unknown as AcceptedMaterialDraft;
    const selected = selectMaterialHypotheses(material, ["h1"]);
    expect(selected.evidence.map(item => item.id)).toEqual(["e1", "e2"]); expect(selected.unresolved_rules).toEqual(["unknown"]); expect(material.hypotheses).toHaveLength(2);
    expect(() => selectMaterialHypotheses(material, [])).toThrow(/至少/);
  });
  it("grounded selection retains linked support and paper-statistic evidence without inventing human review", () => {
    const material = { title: "Grounded", grounding_version: 1, evidence: [{ id: "e1" }, { id: "e2" }, { id: "stat" }],
      hypotheses: [{ id: "h1", evidence_ids: ["e1"], required_data: [] }, { id: "h2", evidence_ids: ["e2"], required_data: [] }],
      support_bindings: [{ hypothesis_id: "h1", evidence_ids: ["e1"] }, { hypothesis_id: "h2", evidence_ids: ["e2"] }],
      paper_statistics: [{ label: "Original number", evidence_ids: ["stat"] }], unresolved_rules: ["unknown"], limitations: [] } as unknown as AcceptedGroundedMaterialDraft;
    const selected = selectMaterialHypotheses(material, ["h1"]) as AcceptedGroundedMaterialDraft;
    expect(selected.support_bindings.map(item => item.hypothesis_id)).toEqual(["h1"]);
    expect(selected.evidence.map(item => item.id)).toEqual(["e1", "stat"]);
    expect(selected.paper_statistics).toEqual(material.paper_statistics);
  });
});

describe("bounded formula-engineering recovery and provider observations", () => {
  const policy = { schema_version: 1, kind: "daily_formula_engineering_v1", operator_semantics_version: "local-panel-v3-scaled-corr-exact-boundaries", allow_field_substitution: false, evaluation_period: "validation" };
  const response = { version: { id: "engineering-version", material_id: "material", digest: "new-digest", actor: "automation" }, method_policy: policy,
    verification_scope: "formula_engineering_only", human_review_status: "pending_human", human_approval: false } as unknown as PublicMaterialEngineering;
  it("persists server-confirmed engineering policy, invalidates old candidates and safely replays the same request", () => {
    const store = storage(); draft(store);
    const before = emptyCreationFlow();
    const after = applyEngineeringConfiguration({ ...before, generation: { inputKey: "old", sourceDigest: "old", sessionId: "old" }, experimentId: "old-experiment" }, response);
    persistCreationFlow(store, key, after, before);
    const restored = JSON.parse(store.getItem(key)!).value.creationFlow;
    expect(isResearchCreationFlow(restored)).toBe(true); expect(restored.engineeringMethodPolicy).toEqual(policy);
    expect(restored.generation).toBeUndefined(); expect(restored.experimentId).toBeUndefined();
    const receiptKey = creationFlowMutationKey("workspace", key);
    const receipt = stageMutation(store, receiptKey, "/research-v2/materials/material/engineering", { expected_digest: "old-digest", unresolved_dispositions: [] }, "engineering-request", { action: "configure_engineering", inputKey: "original" });
    expect(readCreationFlowMutation(store, receiptKey)?.body).toEqual(receipt.body);
    expect(() => applyEngineeringConfiguration(before, { ...response, human_approval: true } as unknown as PublicMaterialEngineering)).toThrow(/范围不一致/);
    expect(isResearchCreationFlow({ ...after, engineeringMethodPolicy: { ...policy, allow_field_substitution: true } })).toBe(false);
  });
  it("distinguishes fixture, unobserved requests and native invocation receipts without claiming real inference", () => {
    const proposal = { provider_mode: "codex_cli", attempts: [] } as unknown as ResearchV2ProposalDetail;
    expect(proposalExecutionKind(proposal)).toBe("not_observed");
    proposal.attempts = [{ receipt: { invocation_started: true, fixture: false } }] as ResearchV2ProposalDetail["attempts"];
    expect(proposalExecutionKind(proposal)).toBe("native_provider_invocation");
    expect(proposalExecutionKind({ ...proposal, provider_mode: "fixture" })).toBe("fixture");
  });
});

describe("generation handoff reconciliation", () => {
  it("reconciles only the current proposal reservation and treats finished receipts as read-only", async () => {
    const { generationReconciliationToken } = await import("./researchCreationFlow");
    const session = { reservations: [{ action: "generate", token: "generation-token", status: "effect_recorded", children: { proposal_id: "proposal" } }] } as unknown as import("./generated/api-contract").PublicResearchSession;
    expect(generationReconciliationToken(session, "proposal")).toBe("generation-token");
    expect(() => generationReconciliationToken(session, "another-proposal")).toThrow(/不唯一匹配/);
    session.reservations[0].status = "finished";
    expect(generationReconciliationToken(session, "proposal")).toBeNull();
  });
  it("keeps the recovery nonce locally and replays the exact token-only API body", async () => {
    const { creationMutationBody } = await import("./researchCreationFlow");
    const store = storage(), receiptKey = creationFlowMutationKey("workspace", key);
    const receipt = stageMutation(store, receiptKey, "/research-v2/sessions/session/reconcile", { token: "original-token" }, "local-nonce", { action: "reconcile_session", inputKey: "unchanged" });
    expect(readCreationFlowMutation(store, receiptKey)?.body.idempotency_key).toBe("local-nonce");
    expect(creationMutationBody(receipt)).toEqual({ token: "original-token" });
    expect(creationMutationBody(readCreationFlowMutation(store, receiptKey)!)).toEqual({ token: "original-token" });
    expect(() => creationMutationBody({ ...receipt, path: "/research-v2/experiments" })).toThrow(/格式不匹配/);
  });
});
