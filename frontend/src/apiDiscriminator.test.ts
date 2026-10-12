import { describe, expect, it, vi, afterEach } from "vitest";
import { ApiContractError, assertApiResponse, contractRoutes } from "./generated/api-contract";
import { api } from "./api";
import fixture from "./fixtures/researchLoopDiscriminatorV2.json";

const copy = <T,>(value: T): T => structuredClone(value);
const loop = () => copy(fixture.loop_v2);
const legacy = () => copy(fixture.legacy_loop_v1);
// Deliberate closed-shape fixtures only; these identifiers confer no authority.
const experiment = () => {
  const saved = loop(); const pin = "a".repeat(64);
  return { schema_version: 2, id: "experiment-shape-fixture", session_id: saved.session_id,
    candidate_source: { kind: "host_corrected_rejected_candidate", loop_id: saved.id,
      correction_action_sequence: 1, intent_digest: pin, correction_receipt_digest: pin,
      parent_proposal_id: "proposal_" + pin, parent_attempt_id: "proposal_attempt_" + pin,
      parent_response_digest: pin, accepted_task_digest: pin },
    request_digest: pin, task_digest: pin, source_kind: "research_revision", created_at: saved.created_at,
    budget: saved.policy.experiment_budget, bootstrap: null, job: null, session: saved.session,
    reservation: { token: "shape-token", sequence: 1, action: "experiment", effect_key: "shape-effect",
      action_digest: pin, source: saved.session.current_source, created_at: saved.created_at,
      deadline_at: saved.session.deadline_at, charges: { tool_actions: 1, provider_dispatches: 0, experiments: 1, failures: 0 },
      status: "reserved", children: null, outcome: null }, candidate_checks: [], semantic_fidelity: "unverified",
    budget_scope: "session_top_level_actions_and_experiments_with_separate_job_steps", public_projection_redacted: false };
};
afterEach(() => vi.unstubAllGlobals());

describe("numeric public schema-version discriminators", () => {
  it("accepts the saved genuine Loop POST201 response", () => {
    expect(fixture.provenance.loop_v2_is_saved_response).toBe(true);
    expect(() => assertApiResponse("/api/research-v2/loops", "POST", 201, loop())).not.toThrow();
  });
  it("also accepts that same saved Loop on GET and advance routes", () => {
    expect(() => assertApiResponse("/api/research-v2/loops/example", "GET", 200, loop())).not.toThrow();
    expect(() => assertApiResponse("/api/research-v2/loops/example/advance", "POST", 200, loop())).not.toThrow();
  });
  it("validates a numeric v2 discriminator inside a paginated response", () => {
    expect(() => assertApiResponse("/api/research-v2/loops?limit=10", "GET", 200,
      { items: [loop()], total: 1, limit: 10, offset: 0 })).not.toThrow();
  });
  it("keeps legacy numeric v1 response validation", () => {
    expect(fixture.provenance.legacy_v1_is_shape_fixture).toBe(true);
    expect(() => assertApiResponse("/api/research-v2/loops", "POST", 201, legacy())).not.toThrow();
  });
  it("accepts a page containing both exact versioned DTO shapes", () => {
    expect(() => assertApiResponse("/api/research-v2/loops", "GET", 200,
      { items: [legacy(), loop()], total: 2, limit: 10, offset: 0 })).not.toThrow();
  });
  it("allows the ordinary API helper to acknowledge a saved successful POST", async () => {
    const saved = loop();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(saved), { status: 201 })));
    await expect(api("/research-v2/loops", { method: "POST" })).resolves.toEqual(saved);
  });
  it("validates the numeric v2 Experiment union through POST and GET", () => {
    expect(() => assertApiResponse("/api/research-v2/experiments", "POST", 201, experiment())).not.toThrow();
    expect(() => assertApiResponse("/api/research-v2/experiments/example", "GET", 200, experiment())).not.toThrow();
  });
  it("preserves legacy v1 Experiment fields rather than inventing a corrected source", () => {
    const { candidate_source: _source, ...common } = experiment();
    const value = { ...common, schema_version: 1, proposal_id: "shape-proposal", accepted_response_digest: "a".repeat(64) };
    expect(() => assertApiResponse("/api/research-v2/experiments", "POST", 201, value)).not.toThrow();
    expect(() => assertApiResponse("/api/research-v2/experiments", "POST", 201,
      { ...value, candidate_source: _source })).toThrow(ApiContractError);
  });
  it("rejects numeric-string and boolean versions in Experiment unions too", () => {
    for (const version of ["2", true, null, 3, 2.5]) {
      expect(() => assertApiResponse("/api/research-v2/experiments", "POST", 201,
        { ...experiment(), schema_version: version })).toThrow(ApiContractError);
    }
  });
  it.each([true, false, null, undefined, "1", "2", "02", "unknown", 0, 3,
    2.5, NaN, Infinity, -Infinity, {}, []])("rejects an invalid version %s without coercing its DTO", (version) => {
    expect(() => assertApiResponse("/api/research-v2/loops", "POST", 201,
      { ...loop(), schema_version: version })).toThrow(ApiContractError);
  });
  it("rejects a nested invalid version", () => {
    expect(() => assertApiResponse("/api/research-v2/loops", "GET", 200,
      { items: [{ ...loop(), schema_version: true }], total: 1, limit: 10, offset: 0 })).toThrow(ApiContractError);
  });
  it("rejects mixing a v2 policy into a v1 DTO", () => {
    expect(() => assertApiResponse("/api/research-v2/loops", "POST", 201,
      { ...loop(), schema_version: 1 })).toThrow(ApiContractError);
  });
  it("keeps unknown-property and required-property rejection", () => {
    expect(() => assertApiResponse("/api/research-v2/loops", "POST", 201,
      { ...loop(), invented_authority: true })).toThrow(ApiContractError);
    const missing: Record<string, unknown> = loop(); delete missing.request_digest;
    expect(() => assertApiResponse("/api/research-v2/loops", "POST", 201, missing)).toThrow(ApiContractError);
  });
  it("rejects multiple matching oneOf alternatives even with a valid integer mapping", () => {
    const route = contractRoutes.find(row => row.path === "/api/research-v2/loops" && row.method === "POST")!;
    const schema = route.responses["201"];
    const original = schema.oneOf;
    try {
      schema.oneOf = [{ $ref: "#/components/schemas/PublicResearchLoopV2" },
        { $ref: "#/components/schemas/PublicResearchLoopV2" }];
      expect(() => assertApiResponse("/api/research-v2/loops", "POST", 201, loop())).toThrow(ApiContractError);
    } finally { schema.oneOf = original; }
  });
  it("uses own mapping keys, rejecting an inherited JavaScript property name", () => {
    expect(() => assertApiResponse("/api/research-v2/loops", "POST", 201,
      { ...loop(), schema_version: "__proto__" })).toThrow(ApiContractError);
  });
});
