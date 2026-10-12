import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import type { LoopSourceSupport, ProvidersResponse, PublicResearchLoop, PublicResearchLoopV2, PublicResearchSession } from "./generated/api-contract";
import {
  CORRECTION_BASE, CORRECTION_POLICY, clearCorrectionPosition, correctionCanAdvance, correctionLoopRequest,
  correctionMutationKey, correctionNextLabel, correctionPendingPlan, correctionPositionFromLoop, correctionPositionKey,
  correctionReviewTarget, correctionSessionRequest, correctionStartPlan, loopPolls, prepareCorrectionAdvance,
  readCorrectionPosition, requireCorrectionLoop, requireCorrectionSession, saveCorrectionPosition,
} from "./researchCorrectionLoop";
import type { CorrectionPosition } from "./researchCorrectionLoop";
import { acknowledgeMutation, readMutation, rejectMutation, stageMutation } from "./reviewRecovery";
import ResearchCorrectionLoop from "./components/ResearchCorrectionLoop";
import { WorkspaceRequestScope } from "./workspaceScope";

const scope = {workspaceId: "workspace", researchId: "research", providerConfigId: "fixture"};
const providers: ProvidersResponse = {schema_version: 1, allow_fixture: true, allow_real: false,
  real_dispatch_enabled: false, dispatch_paused: false, providers: [{config_id: "fixture", mode: "fixture", enabled: true,
    dispatchable: true, model: "synthetic-alpha006-window-v1", reasoning_effort: "low", expected_cli_version: "0.145.0",
    compatibility_verified: false, tool_boundary_verified: false, inference_verified: false, readiness: null, error: null}]};
const support: LoopSourceSupport = {schema_version: 2, research_id: "research", base_revision_id: "base", source_digest: "1".repeat(64),
  correction_supported: true, support_status: "supported", reason_code: "CORRECTION_SOURCE_SUPPORTED", message: "engineering fixture",
  accepted_source_kind: "research_revision", accepted_task_schema_version: 1, accepted_data_kind: "synthetic", candidate_limit: 1,
  max_deterministic_passes: 1, correction_modes: ["deterministic"], policy_ids: [CORRECTION_POLICY],
  policy_references: [{policy_id: CORRECTION_POLICY, policy_digest: "2".repeat(64)}], provider_generation_capability: null,
  provider_config_digest: "3".repeat(64), generation_available: true, provider_optional_qualified: false,
  real_dispatch_qualified: false, public_projection_redacted: false};
const source = {kind: "research_revision" as const, research_id: "research", revision_id: "base", source_digest: "1".repeat(64),
  revision_digest: "4".repeat(64), paper_id: null, selection_digest: null, material_id: null, version_id: null, material_digest: null};
const session: PublicResearchSession = {schema_version: 2, id: "actual-session", state_digest: "5".repeat(64), version: 1,
  purpose: "engineering fixture", config: {provider_config_id: "fixture", provider_config_digest: "3".repeat(64), contract_id: "research_provider_v2"},
  created_at: "2026-10-11T00:00:00Z", deadline_at: "2026-10-11T00:10:00Z", root_source: source, current_source: source,
  budget: {max_provider_dispatches: 1, max_tool_actions: 3, max_experiments: 1, max_failures: 3, max_seconds: 600},
  usage: {provider_dispatches: 0, tool_actions: 0, experiments: 0, failures: 0}, state: "created", effective_state: "created",
  stop_reason: null, remaining_seconds: 600, reservations: [], semantic_fidelity: "unverified", execution_scope: "provider_and_experiment_gated", public_projection_redacted: false};
const plan = correctionStartPlan("research", "base", "fixture", support, providers, true);
const position: CorrectionPosition = {...scope, version: 1, sessionId: session.id, loopId: "loop", plan};
const loop: PublicResearchLoopV2 = {schema_version: 2, id: "loop", session_id: session.id, created_at: session.created_at,
  request_digest: "6".repeat(64), state: "ready", reason: null, next_action: "correction.apply",
  policy: {max_repairs: 1, allow_experiment: true, experiment_budget: {max_steps: 10, max_failures: 2, max_seconds: 180},
    max_experiments: 1, max_actions: 24, correction_policy_id: CORRECTION_POLICY, correction_policy_digest: "2".repeat(64),
    correction_mode: "deterministic", max_deterministic_passes: 1}, actions: [], proposals: [], experiment: null,
  decision: null, decision_applied: false, session, semantic_fidelity: "unverified", real_dispatch_qualified: false, public_projection_redacted: false};
function memory() {
  const values = new Map<string, string>();
  return {values, getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {values.set(key, value);}, removeItem: (key: string) => {values.delete(key);}};
}

describe("Correction setup uses host metadata and actual Session identities", () => {
  it("requests a closed research_revision session without provider configuration or qualification claims", () => {
    const body = correctionSessionRequest(scope, plan);
    expect(body.source).toEqual({kind: "research_revision", research_id: "research", base_revision_id: "base"});
    expect(body.expected_source_digest).toBe(support.source_digest);
    expect(body.expected_provider_config_digest).toBe(support.provider_config_digest);
    expect(body.host_grant_id).toBeNull();
    for (const key of ["executable", "model", "allow_real", "qualified", "policy_rules", "session_id"]) expect(body).not.toHaveProperty(key);
  });
  it("pairs explicit experiment permission and separate finite Session/experiment budgets", () => {
    const without = correctionStartPlan("research", "base", "fixture", support, providers, false);
    expect(correctionSessionRequest(scope, without).budget).toMatchObject({max_experiments: 0, max_tool_actions: 2, max_provider_dispatches: 1});
    const withoutPosition = {...position, plan: without};
    const body = correctionLoopRequest(session, scope, withoutPosition, "proposal-request-key");
    expect(body.allow_experiment).toBe(false); expect(body.experiment_budget).toBeNull();
    expect(correctionSessionRequest(scope, plan).budget).toMatchObject({max_experiments: 1, max_tool_actions: 3, max_seconds: 600});
  });
  it("rejects stale source metadata, unavailable generation and unsupported lanes", () => {
    for (const changed of [{...support, research_id: "other"}, {...support, base_revision_id: "old"},
      {...support, generation_available: false}, {...support, correction_supported: false},
      {...support, support_status: "unsupported" as const}, {...support, source_digest: null},
      {...support, candidate_limit: 2} as unknown as LoopSourceSupport]) {
      expect(() => correctionStartPlan("research", "base", "fixture", changed, providers, true)).toThrow();
    }
  });
  it("does not treat Native verification booleans or host real permission as a synthetic grant", () => {
    const native = {...providers, allow_real: true, real_dispatch_enabled: true,
      providers: [{...providers.providers[0], mode: "codex_cli" as const, compatibility_verified: true, inference_verified: true, tool_boundary_verified: true}]};
    expect(() => correctionStartPlan("research", "base", "fixture", support, native, true)).toThrow("合成示例");
    expect(() => correctionStartPlan("research", "base", "fixture", support, {...providers, allow_fixture: false}, true)).toThrow();
    expect(() => correctionStartPlan("research", "base", "fixture", support, {...providers, dispatch_paused: true}, true)).toThrow();
  });
  it("requires one actual host policy reference instead of client rules or a default digest", () => {
    for (const changed of [{...support, policy_references: []}, {...support, policy_ids: []},
      {...support, policy_references: [...support.policy_references, ...support.policy_references]},
      {...support, policy_references: [{policy_id: CORRECTION_POLICY, policy_digest: "invented"}]}] satisfies LoopSourceSupport[])
      expect(() => correctionStartPlan("research", "base", "fixture", changed, providers, true)).toThrow("策略引用");
  });
  it("binds the Loop to the fetched Session state, original source and deterministic policy", () => {
    const body = correctionLoopRequest(session, scope, position, "actual-request-key");
    expect(body).toMatchObject({schema_version: 2, max_repairs: 1, correction_mode: "deterministic",
      correction_policy_id: plan.policyId, correction_policy_digest: plan.policyDigest,
      initial_proposal: {session_id: session.id, expected_session_digest: session.state_digest, candidate_limit: 1,
        expected_source_digest: source.source_digest, operation: "generate_candidates", idempotency_key: "actual-request-key"}});
    expect(body.initial_proposal).not.toHaveProperty("proposal_id");
    expect(body.initial_proposal).not.toHaveProperty("accepted_response_digest");
    expect(body.initial_proposal.budget).toEqual({max_provider_invocations: 1, max_seconds: 120});
  });
  it("rejects another Session, config, source, used slot or closed budget", () => {
    for (const changed of [{...session, id: "other"}, {...session, config: {...session.config, provider_config_digest: "f".repeat(64)}},
      {...session, root_source: {...source, kind: "accepted_material" as const}},
      {...session, current_source: {...source, revision_id: "changed"}}, {...session, remaining_seconds: 0},
      {...session, effective_state: "exhausted" as const}, {...session, usage: {...session.usage, provider_dispatches: 1}}])
      expect(() => correctionLoopRequest(changed, scope, position, "key")).toThrow();
    expect(requireCorrectionSession(session, scope, position)).toBe(session);
  });
  it("recovers the original frozen setup plan without taking new form choices or budget", () => {
    const storage = memory(), key = correctionMutationKey(scope, "session-create");
    const item = stageMutation(storage, key, "/research-v2/sessions", correctionSessionRequest(scope, plan), "original-session-key", {plan});
    expect(correctionPendingPlan(item, scope)).toEqual(plan);
    for (const changed of [{...item, context: {plan: {...plan, allowExperiment: false}}},
      {...item, context: {plan: {...plan, policyDigest: "bad"}}}, {...item, body: {...item.body, expected_source_digest: "f".repeat(64)}},
      {...item, body: {...item.body, source: {kind: "accepted_material"}}}])
      expect(() => correctionPendingPlan(changed, scope)).toThrow("冻结计划");
    expect(storage.getItem(key)).toContain("original-session-key");
  });
});

describe("Correction navigation and unknown writes retain original identities", () => {
  it("stores bounded navigation and frozen intent without inventing a Session or output", () => {
    const storage = memory(); saveCorrectionPosition(storage, position);
    expect(readCorrectionPosition(storage, scope)).toEqual(position);
    const raw = storage.getItem(correctionPositionKey(scope))!;
    for (const name of ["usage", "accepted_expression", "provider_association", "qualification"]) expect(raw).not.toContain(name);
    clearCorrectionPosition(storage, position); expect(readCorrectionPosition(storage, scope)).toBeNull();
  });
  it("fails closed on corrupt or foreign navigation rather than dropping it and creating a new task", () => {
    const storage = memory(), key = correctionPositionKey(scope);
    for (const value of [{...position, version: true}, {...position, workspaceId: "other"},
      {...position, sessionId: ""}, {...position, unknown: true}, {...position, plan: {...plan, policyDigest: "bad"}}]) {
      storage.setItem(key, JSON.stringify(value));
      expect(() => readCorrectionPosition(storage, scope)).toThrow("无法核实");
      expect(storage.getItem(key)).toBe(JSON.stringify(value));
    }
  });
  it("never overwrites another task or a frozen plan in the same Session", () => {
    const storage = memory(); saveCorrectionPosition(storage, position);
    for (const changed of [{...position, sessionId: "another"}, {...position, loopId: "another"},
      {...position, plan: {...plan, allowExperiment: false}}]) expect(() => saveCorrectionPosition(storage, changed)).toThrow();
    expect(() => clearCorrectionPosition(storage, {...position, loopId: "another"})).toThrow("没有清除");
    expect(readCorrectionPosition(storage, scope)).toEqual(position);
  });
  it("requires durable readback before a navigation record can be acknowledged", () => {
    const storage = {getItem: () => null, setItem: () => {}};
    expect(() => saveCorrectionPosition(storage, position)).toThrow("未能确认");
  });
  it("refresh recovery stages the original server action key, never a replacement UUID", () => {
    const storage = memory(), key = correctionMutationKey(scope, "advance", "loop"), path = `${CORRECTION_BASE}/loop/advance`;
    const pendingLoop = {...loop, state: "recovering" as const, actions: [{sequence: 1, idempotency_key: "server-original-key",
      kind: "proposal.create" as const, status: "pending" as const, request_digest: "7".repeat(64), receipt_digest: null,
      planned_at: session.created_at, proposal_id: null, experiment_id: null, step_id: null, experiment_action: null, correction: null}]};
    const item = prepareCorrectionAdvance(storage, key, path, pendingLoop, "fresh-browser-uuid");
    expect(item.body.idempotency_key).toBe("server-original-key");
    expect(item.context).toEqual({loop_id: "loop", action_sequence: 1, action_kind: "proposal.create"});
    expect(prepareCorrectionAdvance(storage, key, path, pendingLoop, "different-uuid")).toEqual(item);
  });
  it("an uncertain POST is replayed unchanged even after the server reached a terminal state", () => {
    const storage = memory(), key = correctionMutationKey(scope, "advance", "loop"), path = `${CORRECTION_BASE}/loop/advance`;
    const item = prepareCorrectionAdvance(storage, key, path, loop, "original-key");
    expect(prepareCorrectionAdvance(storage, key, path, {...loop, state: "awaiting_handoff", next_action: null}, "new-key")).toEqual(item);
    expect(readMutation(storage, key, path)?.body.idempotency_key).toBe("original-key");
    acknowledgeMutation(storage, key, path, item);
    expect(readMutation(storage, key, path)).toBeNull();
  });
  it("an uncertain original request stays replayable after a temporary GET failure, without staging a new action", () => {
    const storage = memory(), key = correctionMutationKey(scope, "advance", "loop"), path = `${CORRECTION_BASE}/loop/advance`;
    expect(() => prepareCorrectionAdvance(storage, key, path, null, "new", "loop")).toThrow("没有可推进");
    expect(storage.getItem(key)).toBeNull();
    const original = prepareCorrectionAdvance(storage, key, path, loop, "original");
    expect(prepareCorrectionAdvance(storage, key, path, null, "replacement", "loop")).toEqual(original);
    expect(() => prepareCorrectionAdvance(storage, key, path, null, "replacement", "foreign")).toThrow("不属于");
  });
  it("foreign, corrupt and explicitly rejected original requests cannot be replaced", () => {
    const storage = memory(), key = correctionMutationKey(scope, "advance", "loop"), path = `${CORRECTION_BASE}/loop/advance`;
    const foreign = stageMutation(storage, key, path, {}, "other-key", {loop_id: "other"});
    expect(() => prepareCorrectionAdvance(storage, key, path, loop, "fresh-key")).toThrow("不属于");
    acknowledgeMutation(storage, key, path, foreign);
    const original = prepareCorrectionAdvance(storage, key, path, loop, "original");
    rejectMutation(storage, key, path, original);
    expect(() => prepareCorrectionAdvance(storage, key, path, loop, "new")).toThrow("明确拒绝");
    expect(readMutation(storage, key, path)?.body.idempotency_key).toBe("original");
  });
});

describe("Public loop display distinguishes effects, waiting and actual review targets", () => {
  it("polling is read only while waiting; terminal states cannot make a new action", () => {
    for (const state of ["waiting_provider", "waiting_execution"] as const) {
      const waiting = {...loop, state, next_action: null}; expect(loopPolls(waiting)).toBe(true); expect(correctionCanAdvance(waiting)).toBe(false);
    }
    for (const state of ["stopped", "exhausted", "interrupted", "awaiting_handoff"] as const) {
      const terminal = {...loop, state, next_action: null}; expect(loopPolls(terminal)).toBe(false); expect(correctionCanAdvance(terminal)).toBe(false);
      expect(() => prepareCorrectionAdvance(memory(), "key", `${CORRECTION_BASE}/loop/advance`, terminal, "new")).toThrow("没有可推进");
    }
    expect(correctionNextLabel(loop)).toContain("一次等价表示修正");
  });
  it("does not reinterpret legacy or mismatched loops as a correction success", () => {
    expect(() => requireCorrectionLoop({...loop, schema_version: 1} as unknown as PublicResearchLoop, scope)).toThrow("旧版本");
    expect(() => requireCorrectionLoop({...loop, id: "other"}, scope, position)).toThrow();
    expect(() => requireCorrectionLoop({...loop, session_id: "other"}, scope)).toThrow();
    expect(() => requireCorrectionLoop(loop, {...scope, researchId: "other"})).toThrow();
    for (const changed of [{...loop, session: {...session, config: {...session.config, provider_config_digest: "f".repeat(64)}}},
      {...loop, session: {...session, root_source: {...source, revision_id: "foreign"}}},
      {...loop, session: {...session, root_source: {...source, source_digest: "f".repeat(64)}}},
      {...loop, policy: {...loop.policy, correction_policy_digest: "f".repeat(64)}},
      {...loop, policy: {...loop.policy, correction_mode: "provider_optional" as const}},
      {...loop, policy: {...loop.policy, allow_experiment: false}}])
      expect(() => requireCorrectionLoop(changed, scope, position)).toThrow();
  });
  it("reads historical loops after an owned revision without making the old source current", () => {
    const history = {...loop, session: {...session, current_source: {...source, revision_id: "owned-new-revision", source_digest: "a".repeat(64)}}};
    expect(requireCorrectionLoop(history, scope, position)).toBe(history);
    expect(correctionPositionFromLoop(history, scope).plan.baseRevisionId).toBe("base");
    expect(correctionPositionFromLoop(history, scope).plan.sourceDigest).toBe(source.source_digest);
  });
  it("only uses actual saved run/job identifiers, without fabricating a Provider accepted response", () => {
    expect(correctionReviewTarget(loop)).toEqual({jobId: null, runId: null});
    const result = {...loop, experiment: {id: "experiment", job_id: "actual-job", run_id: "actual-run", task_digest: "8".repeat(64),
      revision_id: "owned", state: "completed", candidate_source: {kind: "host_corrected_rejected_candidate" as const, loop_id: "loop",
        correction_action_sequence: 2, intent_digest: "9".repeat(64), correction_receipt_digest: "a".repeat(64),
        parent_proposal_id: "proposal_"+"b".repeat(64), parent_attempt_id: "proposal_attempt_"+"c".repeat(64),
        parent_response_digest: "d".repeat(64), accepted_task_digest: "8".repeat(64)}}};
    expect(correctionReviewTarget(result)).toEqual({jobId: "actual-job", runId: "actual-run"});
    expect(result.experiment.candidate_source).not.toHaveProperty("accepted_response_digest");
  });
  it("initial UI explicitly leaves the default provider disabled and explains the portable demo", () => {
    const workspace = new WorkspaceRequestScope(); workspace.acceptHealthRequest(workspace.beginHealthRequest(), "workspace");
    const html = renderToStaticMarkup(createElement(ResearchCorrectionLoop, {workspaceScope: workspace, researchId: "research",
      baseRevisionId: "base", providers: {...providers, allow_fixture: false, providers: []}, providerConfigId: "", onOpenJob: () => {}}));
    expect(html).toContain("默认没有 Provider"); expect(html).toContain("--allow-fixture-provider");
    expect(html).toContain("明确允许随后创建一次合成数据验证期实验"); expect(html).toContain("disabled");
    expect(html).not.toContain("模型已认证"); expect(html).not.toContain("paper_original approved");
  });
});
