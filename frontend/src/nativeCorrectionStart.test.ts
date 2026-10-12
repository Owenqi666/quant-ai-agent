import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { PublicResearchLoopV2, PublicResearchSession } from "./generated/api-contract";
import { CORRECTION_POLICY, correctionPositionKey } from "./researchCorrectionLoop";
import { NATIVE_CORRECTION_BASE, acknowledgeNativeCancel, acknowledgeNativeMutation, nativeCanCancel, nativeCancelJournalKey,
  nativeIdentity, nativeJournalKey, nativeReceiptLinks, pinNativePlan, prepareNativeCancel, prepareNativeMutation,
  readNativeCancelJournal, readNativeJournal, rejectNativeCancel, rejectNativeMutation, requireNativeLoop,
  requireNativeStart, requireNativeSupport, sameNativePlan } from "./nativeCorrectionStart";
import type { NativeCorrectionStartResponse, NativeCorrectionStartSummary, NativeCorrectionSupport } from "./nativeCorrectionStart";
import ResearchCorrectionLoop from "./components/ResearchCorrectionLoop";
import { WorkspaceRequestScope } from "./workspaceScope";
import crossActionSnapshot from "./testFixtures/nativeCorrectionCrossAction.json";
import finalOriginalSnapshot from "./testFixtures/nativeCorrectionFinalOriginal.json";

const scope = {workspaceId: "workspace", researchId: "research", providerConfigId: "fixture"};
const hash = (char: string) => char.repeat(64);
const plan: NativeCorrectionStartSummary = {...nativeIdentity(scope, "base"), start_id: `native_start_${hash("1")}`,
  start_digest: hash("2"), provider_config_digest: hash("3"), source_digest: hash("4"), session_id: "actual-native-session",
  loop_id: `research_loop_${hash("5")}`, loop_request_digest: hash("6"), correction_policy_id: CORRECTION_POLICY,
  correction_policy_digest: hash("7"), state: "prepared", available: true, reason_code: "NATIVE_START_AVAILABLE", message: "offline fixture",
  authorization: {authority_id: "research_loop_native_v1", grant_id: `loop_native_grant_${hash("8")}`, kind: "fixture",
    human_authorization_digest: null, deadline_at: "2030-10-11T12:00:00Z", max_provider_dispatches: 2, max_repairs: 1, candidate_limit: 1},
  evidence: {profile_digest: hash("9"), profile_status: "pinned_static", boundary_status: "fixture_only", inference_status: "unverified", semantic_fidelity: "unverified"},
  development_scope: "ordinary_synthetic_task1_generation1_eligible_repair1", public_projection_redacted: false};
const support: NativeCorrectionSupport = {...nativeIdentity(scope, "base"), available: true, reason_code: "NATIVE_START_AVAILABLE",
  message: "offline fixture", plans: [plan], public_projection_redacted: false};
const source = {kind: "research_revision" as const, research_id: scope.researchId, revision_id: "base", source_digest: plan.source_digest,
  revision_digest: hash("a"), paper_id: null, selection_digest: null, material_id: null, version_id: null, material_digest: null};
const session: PublicResearchSession = {schema_version: 2, id: plan.session_id, state_digest: hash("b"), version: 1,
  purpose: "offline fixture", config: {provider_config_id: scope.providerConfigId, provider_config_digest: plan.provider_config_digest, contract_id: "research_provider_v2"},
  created_at: "2026-10-11T12:00:00Z", deadline_at: plan.authorization.deadline_at, root_source: source, current_source: source,
  budget: {max_provider_dispatches: 2, max_tool_actions: 3, max_experiments: 1, max_failures: 3, max_seconds: 1200},
  usage: {provider_dispatches: 0, tool_actions: 0, experiments: 0, failures: 0}, state: "created", effective_state: "created", stop_reason: null,
  remaining_seconds: 1200, reservations: [], semantic_fidelity: "unverified", execution_scope: "provider_and_experiment_gated", public_projection_redacted: false};
const loop: PublicResearchLoopV2 = {schema_version: 2, id: plan.loop_id, session_id: session.id, request_digest: plan.loop_request_digest,
  created_at: session.created_at, state: "ready", reason: null, next_action: "proposal.create",
  policy: {max_repairs: 1, allow_experiment: true, experiment_budget: {max_steps: 10, max_failures: 2, max_seconds: 180}, max_experiments: 1,
    max_actions: 24, correction_policy_id: CORRECTION_POLICY, correction_policy_digest: plan.correction_policy_digest,
    correction_mode: "provider_optional", max_deterministic_passes: 1}, actions: [], proposals: [], experiment: null,
  decision: null, decision_applied: false, session, semantic_fidelity: "unverified", real_dispatch_qualified: false, public_projection_redacted: false};
function memory() {
  const values = new Map<string, string>();
  return {values, getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {values.set(key, value);}};
}
function pinned(storage = memory()) {
  return {storage, journal: pinNativePlan(storage, scope, readNativeJournal(storage, scope), plan)};
}
function attached() {
  const {storage, journal} = pinned();
  const staged = prepareNativeMutation(storage, scope, journal, "start", loop);
  return {storage, journal: acknowledgeNativeMutation(storage, scope, staged)};
}
describe("Native support and immutable host plan", () => {
  it("uses a closed POST identity and preserves the exact two-slot host scope", () => {
    expect(nativeIdentity(scope, "base")).toEqual({schema_version: 1, research_id: "research", base_revision_id: "base", provider_config_id: "fixture"});
    expect(requireNativeSupport(support, nativeIdentity(scope, "base"))).toBe(support);
    for (const bad of [{...support, schema_version: 2}, {...support, base_revision_id: "different"}, {...support, allow_real: true},
      {...support, plans: [{...plan, authorization: {...plan.authorization, max_provider_dispatches: 3}}]},
      {...support, plans: [{...plan, development_scope: "E01"}]}, {...support, plans: [plan, plan]}])
      expect(() => requireNativeSupport(bad, nativeIdentity(scope, "base"))).toThrow();
  });
  it("READ_BOUNDARY_UNVERIFIED remains an explicit blocker without an inferred qualification", () => {
    const blocked = {...support, available: false, plans: [], reason_code: "READ_BOUNDARY_UNVERIFIED", message: "read boundary failed"};
    expect(requireNativeSupport(blocked, nativeIdentity(scope, "base"))).toEqual(blocked);
    expect(plan.evidence.profile_status).toBe("pinned_static"); expect(plan.evidence.inference_status).toBe("unverified");
    const storage = memory();
    expect(() => pinNativePlan(storage, scope, readNativeJournal(storage, scope), {...plan, available: false, state: "unknown"})).toThrow();
    expect(readNativeJournal(storage, scope).position).toBeNull();
  });
  it("can read updated host state without accepting source/config/profile/authorization drift", () => {
    expect(sameNativePlan(plan, {...plan, state: "active", evidence: {...plan.evidence, inference_status: "fixture_observed"}})).toBe(true);
    for (const changed of [{...plan, source_digest: hash("c")}, {...plan, provider_config_digest: hash("c")},
      {...plan, evidence: {...plan.evidence, profile_digest: hash("c")}}, {...plan, authorization: {...plan.authorization, grant_id: `loop_native_grant_${hash("c")}`}}])
      expect(sameNativePlan(plan, changed)).toBe(false);
  });
});
describe("Independent versioned Native recovery journal", () => {
  it("does not read, migrate or write legacy deterministic storage", () => {
    const storage = memory(), legacyKey = correctionPositionKey(scope), legacy = '{"version":1,"legacy":"keep byte for byte"}';
    storage.setItem(legacyKey, legacy);
    const {journal} = pinned(storage); expect(journal.position?.plan).toEqual(plan);
    expect(nativeJournalKey(scope)).not.toContain("recovery:v1"); expect(storage.getItem(legacyKey)).toBe(legacy);
  });
  it("pins plan before POST and start only carries immutable identity, start pins and original key", () => {
    const {storage, journal} = pinned(), staged = prepareNativeMutation(storage, scope, journal, "start", loop);
    expect(staged.pending?.path).toBe(`${NATIVE_CORRECTION_BASE}/start`);
    expect(Object.keys(staged.pending!.body).sort()).toEqual(["schema_version", "research_id", "base_revision_id", "provider_config_id", "start_id", "start_digest", "idempotency_key"].sort());
    expect(readNativeJournal(storage, scope)).toEqual(staged);
    expect(prepareNativeMutation(storage, scope, staged, "start", null)).toEqual(staged);
    expect(staged.position?.attached).toBe(false); expect(loop.session.usage.provider_dispatches).toBe(0);
  });
  it("survives lost ACK, refresh and terminal response by replaying the exact original start", () => {
    const {storage, journal} = pinned(), original = prepareNativeMutation(storage, scope, journal, "start", loop);
    const reload = readNativeJournal(storage, scope), terminal = {...loop, state: "awaiting_handoff" as const, next_action: null};
    expect(prepareNativeMutation(storage, scope, reload, "start", terminal).pending).toEqual(original.pending);
    const response: NativeCorrectionStartResponse = {schema_version: 1, start_id: plan.start_id, start_digest: plan.start_digest,
      idempotency_key: original.pending!.body.idempotency_key, attached_existing: true, plan: {...plan, state: "terminal"}, loop: terminal};
    expect(requireNativeStart(response, original)).toBe(terminal);
    const confirmed = acknowledgeNativeMutation(storage, scope, original);
    expect(confirmed.position?.attached).toBe(true); expect(confirmed.pending).toBeNull(); expect(confirmed.position?.plan).toEqual(plan);
    expect(() => requireNativeStart({...response, idempotency_key: "another-key"}, original)).toThrow();
    expect(() => requireNativeStart({...response, attached_existing: false} as unknown as NativeCorrectionStartResponse, original)).toThrow();
  });
  it("retains unknown/rejected tasks and refuses an automatic replacement plan", () => {
    const {storage, journal} = pinned(), original = prepareNativeMutation(storage, scope, journal, "start", loop);
    const rejected = rejectNativeMutation(storage, scope, original);
    expect(() => prepareNativeMutation(storage, scope, rejected, "start", loop)).toThrow();
    expect(() => pinNativePlan(storage, scope, rejected, {...plan, start_id: `native_start_${hash("c")}`, start_digest: hash("c")})).toThrow();
    expect(readNativeJournal(storage, scope).pending?.body.idempotency_key).toBe(original.pending?.body.idempotency_key);
  });
  it("detects another tab's stale write before a new mutation and converges to a stable same-step key", () => {
    const {storage, journal} = pinned(), tab2 = readNativeJournal(storage, scope);
    const original = prepareNativeMutation(storage, scope, journal, "start", loop);
    expect(() => prepareNativeMutation(storage, scope, tab2, "start", loop)).toThrow("另一标签页");
    expect(prepareNativeMutation(storage, scope, readNativeJournal(storage, scope), "start", null)).toEqual(original);
    const second = pinned(), independent = prepareNativeMutation(second.storage, scope, second.journal, "start", loop);
    expect(independent.pending?.body.idempotency_key).toBe(original.pending?.body.idempotency_key);
  });
  it("fails closed on corrupt/version/foreign records without deleting them", () => {
    for (const raw of ['{invalid', JSON.stringify({version: 2}), JSON.stringify({...pinned().journal, scope: "foreign"}),
      JSON.stringify({...pinned().journal, position: {...pinned().journal.position, plan: {...plan, root_grant_path: "/private"}}})]) {
      const storage = memory(); storage.setItem(nativeJournalKey(scope), raw);
      expect(() => readNativeJournal(storage, scope)).toThrow(); expect(storage.getItem(nativeJournalKey(scope))).toBe(raw);
    }
  });
  it("does not dispatch when storage fails and retains pending after a failed ACK write", () => {
    const {storage, journal} = pinned();
    const denied = {...storage, setItem: () => {throw new Error("quota denied");}};
    expect(() => prepareNativeMutation(denied, scope, journal, "start", loop)).toThrow("quota denied");
    expect(readNativeJournal(storage, scope).pending).toBeNull();
    const original = prepareNativeMutation(storage, scope, journal, "start", loop);
    expect(() => acknowledgeNativeMutation(denied, scope, original)).toThrow("quota denied");
    expect(readNativeJournal(storage, scope)).toEqual(original);
    const nonDurable = {...storage, setItem: () => {}};
    expect(() => acknowledgeNativeMutation(nonDurable, scope, original)).toThrow("本机保存");
  });
});
describe("Original Loop control and actual Native receipt links", () => {
  it("keeps same Loop/session and provider_optional budget2 separate from deterministic budget1", () => {
    const {journal} = attached(); expect(requireNativeLoop(loop, journal.position!)).toBe(loop);
    for (const changed of [{...loop, id: "other"}, {...loop, request_digest: hash("c")}, {...loop, session_id: "other"},
      {...loop, policy: {...loop.policy, correction_mode: "deterministic" as const}},
      {...loop, session: {...session, budget: {...session.budget, max_provider_dispatches: 1}}},
      {...loop, session: {...session, root_source: {...source, revision_id: "different"}}}])
      expect(() => requireNativeLoop(changed, journal.position!)).toThrow();
    expect(requireNativeLoop({...loop, session: {...session, current_source: {...source, revision_id: "owned-repaired"}}}, journal.position!)).toBeTruthy();
  });
  it("uses original saved pending ledger key, and replays it unchanged after failed GET or stopped loop", () => {
    const {storage, journal} = attached();
    const serverPending = {...loop, state: "recovering" as const, actions: [{sequence: 3, idempotency_key: "original-server-key", kind: "proposal.create" as const,
      experiment_action: null, planned_at: session.created_at, status: "pending" as const, request_digest: hash("c"), receipt_digest: null,
      proposal_id: null, experiment_id: null, step_id: null, correction: null}]};
    const original = prepareNativeMutation(storage, scope, journal, "advance", serverPending);
    expect(original.pending?.body).toEqual({idempotency_key: "original-server-key"});
    expect(prepareNativeMutation(storage, scope, original, "advance", null)).toEqual(original);
    expect(() => prepareNativeMutation(storage, scope, original, "cancel", loop)).toThrow();
  });
  it("stages cancel on original route and blocks new advances for unknown/terminal state", () => {
    const {storage, journal} = attached(), cancel = prepareNativeMutation(storage, scope, journal, "cancel", loop);
    expect(cancel.pending?.path).toBe(`/research-v2/loops/${plan.loop_id}/cancel`);
    for (const state of ["awaiting_handoff", "interrupted", "exhausted"] as const) {
      const other = attached(); expect(() => prepareNativeMutation(other.storage, scope, other.journal, "advance", {...loop, state, next_action: null})).toThrow();
    }
  });
  it("pairs the actual original create/reconcile actions once without changing the saved loop", () => {
    const original = structuredClone(crossActionSnapshot) as PublicResearchLoopV2, before = JSON.stringify(original);
    expect(original.actions[2].kind).toBe("proposal.create");
    expect(original.actions[3].kind).toBe("proposal.reconcile");
    const links = nativeReceiptLinks(original);
    expect(links).toHaveLength(1);
    expect(links[0]).toMatchObject({sequence: 3, status: "completed",
      intent: {parent: {proposal_id: original.proposals[0].id}},
      receipt: {provider_association: {proposal_id: original.proposals[1].id}}});
    expect(JSON.stringify(original)).toBe(before);
    expect(nativeReceiptLinks({...original, actions: [...original.actions].reverse()})).toEqual(links);
  });
  it("preserves the actual completed receipt after Experiment commits a new current revision", () => {
    const final = structuredClone(finalOriginalSnapshot) as PublicResearchLoopV2;
    expect(final.experiment?.state).toBe("completed");
    expect(final.session.current_source.source_digest).not.toBe(final.session.root_source.source_digest);
    expect(nativeReceiptLinks(final)).toMatchObject([{status: "completed", sequence: 3}]);
    const advanced = structuredClone(final); advanced.session.current_source.source_digest = hash("a");
    expect(nativeReceiptLinks(advanced)).toEqual(nativeReceiptLinks(final));
    for (const field of ["root", "parent reservation", "child reservation"] as const) {
      const foreign = structuredClone(final);
      if (field === "root") foreign.session.root_source.source_digest = hash("a");
      else foreign.session.reservations[field === "parent reservation" ? 0 : 1].source.source_digest = hash("a");
      expect(nativeReceiptLinks(foreign).some(r => r.status !== "unknown")).toBe(false);
    }
  });
  it("retains a genuinely intent-only historical task as unknown", () => {
    const original = structuredClone(crossActionSnapshot) as PublicResearchLoopV2;
    original.actions = original.actions.slice(0, 3);
    expect(nativeReceiptLinks(original)).toMatchObject([{sequence: 3, status: "unknown", receipt: null}]);
  });
  const foreignBindings: [string, (original: PublicResearchLoopV2) => void][] = [
    ["parent proposal", l => {l.actions[2].provider_correction!.parent.proposal_id = "foreign-parent";}],
    ["parent attempt", l => {l.actions[2].provider_correction!.parent.attempt_id = "foreign-attempt";}],
    ["parent response digest", l => {l.actions[2].provider_correction!.parent.response_digest = hash("a");}],
    ["parent binding digest", l => {l.actions[3].provider_correction_receipt!.parent_binding_digest = hash("a");}],
    ["policy", l => {l.actions[3].provider_correction_receipt!.policy_id = "integral-window-representation-v1";}],
    ["source", l => {l.actions[3].provider_correction_receipt!.source_digest = hash("a");}],
    ["diagnostics", l => {l.actions[3].provider_correction_receipt!.diagnostics_digest = hash("a");}],
    ["candidate", l => {l.actions[3].provider_correction_receipt!.candidate_id = "foreign-candidate";}],
    ["child proposal", l => {l.actions[3].provider_correction_receipt!.provider_association!.proposal_id = "foreign-child";}],
    ["child attempt", l => {l.actions[3].provider_correction_receipt!.provider_association!.attempt_id = "foreign-attempt";}],
    ["child response digest", l => {l.actions[3].provider_correction_receipt!.provider_association!.response_digest = hash("a");}],
  ];
  it.each(foreignBindings)("keeps a foreign %s unknown instead of accepting a complete pair", (_, alter) => {
    const original = structuredClone(crossActionSnapshot) as PublicResearchLoopV2; alter(original);
    expect(nativeReceiptLinks(original).some(r => r.status !== "unknown")).toBe(false);
  });
  it("keeps orphan receipts and ambiguous duplicate bindings unknown", () => {
    const original = structuredClone(crossActionSnapshot) as PublicResearchLoopV2;
    const orphan = structuredClone(original); orphan.actions[2].provider_correction = null;
    expect(nativeReceiptLinks(orphan)).toMatchObject([{sequence: 4, intent: null, status: "unknown"}]);
    const duplicateIntent = structuredClone(original); duplicateIntent.actions.push({...duplicateIntent.actions[2], sequence: 2});
    const duplicateReceipt = structuredClone(original); duplicateReceipt.actions.push({...duplicateReceipt.actions[3], sequence: 5});
    const duplicateChild = structuredClone(original); duplicateChild.proposals.push({...duplicateChild.proposals[1]});
    for (const ambiguous of [duplicateIntent, duplicateReceipt, duplicateChild])
      expect(nativeReceiptLinks(ambiguous).some(r => r.status !== "unknown")).toBe(false);
  });
  it("defaults a codex provider to Native without advertising acceptance or creating a session", () => {
    const workspaceScope = new WorkspaceRequestScope(); workspaceScope.acceptHealthRequest(workspaceScope.beginHealthRequest(), scope.workspaceId);
    const html = renderToStaticMarkup(createElement(ResearchCorrectionLoop, {workspaceScope, researchId: scope.researchId,
      baseRevisionId: "base", providerConfigId: "native", providers: {schema_version: 1, allow_fixture: false, allow_real: true,
        real_dispatch_enabled: true, dispatch_paused: false, providers: [{config_id: "native", mode: "codex_cli", enabled: true, dispatchable: true,
          model: "pinned", reasoning_effort: "low", expected_cli_version: "pinned", compatibility_verified: true,
          tool_boundary_verified: false, inference_verified: false, readiness: null, error: null}]}, onOpenJob: () => {}}));
    expect(html).toContain("宿主批准 Native 修正任务"); expect(html).toContain("实际读取边界尚未验收");
    expect(html).not.toContain("创建有界研究会话"); expect(html).not.toContain("模型已认证");
  });
});

describe("Independent safe stop preserves uncertain original steps", () => {
  const uncertainLoop = {...loop, state: "interrupted" as const, next_action: null,
    session: {...session, state: "active" as const, effective_state: "active" as const,
      usage: {...session.usage, provider_dispatches: 1}, reservations: [{token: "unknown-generation-reservation", sequence: 1,
        action: "generate" as const, effect_key: "original-generation-key", action_digest: hash("a"), source,
        created_at: session.created_at, deadline_at: session.deadline_at, charges: {...session.usage, provider_dispatches: 1},
        status: "effect_recorded" as const, children: {proposal_id: "unknown-proposal", research_job_id: null, run_id: null}, outcome: null}]}};
  const cancelledUnknown = {...uncertainLoop, session: {...uncertainLoop.session,
    state: "cancelled" as const, effective_state: "cancelled" as const, stop_reason: "USER_CANCELLED",
    reservations: uncertainLoop.session.reservations.map(r => ({...r, status: "finished" as const,
      outcome: {status: "interrupted" as const, code: "UNKNOWN_PROVIDER_OUTCOME", message: "Unknown effect remains unknown", result_digest: null}}))}};
  function originalAdvance(rejected: boolean) {
    const {storage, journal} = attached(), staged = prepareNativeMutation(storage, scope, journal, "advance", loop);
    return {storage, journal: rejected ? rejectNativeMutation(storage, scope, staged) : staged};
  }
  it("uses the original Session state, allowing interrupted Loop with active Session and blocking actual terminal Sessions", () => {
    const {journal} = attached();
    expect(nativeCanCancel(uncertainLoop, journal.position!)).toBe(true);
    for (const effective_state of ["created", "active", "awaiting_input", "awaiting_handoff"] as const)
      expect(nativeCanCancel({...uncertainLoop, session: {...session, effective_state}}, journal.position!)).toBe(true);
    for (const effective_state of ["completed", "stopped", "cancelled", "exhausted", "interrupted"] as const)
      expect(nativeCanCancel({...loop, session: {...session, effective_state}}, journal.position!)).toBe(false);
    expect(nativeCanCancel(uncertainLoop, {...journal.position!, attached: false})).toBe(false);
    expect(() => nativeCanCancel({...uncertainLoop, id: "foreign"}, journal.position!)).toThrow();
  });
  it("safely stops unknown and rejected advances without changing any original v1 bytes, key or state", () => {
    for (const rejected of [false, true]) {
      const {storage, journal} = originalAdvance(rejected), position = journal.position!, before = storage.getItem(nativeJournalKey(scope));
      const stop = prepareNativeCancel(storage, scope, journal, readNativeCancelJournal(storage, scope, position), uncertainLoop);
      expect(stop.pending?.path).toBe(`/research-v2/loops/${plan.loop_id}/cancel`);
      expect(stop.pending?.body.idempotency_key).not.toBe(journal.pending!.body.idempotency_key);
      expect(storage.getItem(nativeJournalKey(scope))).toBe(before);
      const ack = acknowledgeNativeCancel(storage, scope, position, stop, cancelledUnknown);
      expect(ack.acknowledged).toBe(true); expect(ack.pending?.body).toEqual(stop.pending?.body);
      expect(storage.getItem(nativeJournalKey(scope))).toBe(before);
      expect(readNativeJournal(storage, scope).pending).toEqual(journal.pending);
      expect(cancelledUnknown.state).toBe("interrupted"); expect(cancelledUnknown.session.usage.provider_dispatches).toBe(1);
      expect(cancelledUnknown.session.reservations[0].outcome.status).toBe("interrupted");
      expect(cancelledUnknown.session.reservations[0].outcome.result_digest).toBeNull();
    }
  });
  it("lost stop ACK replays only its durable original key after refresh, without advancing or clearing old pending", () => {
    const {storage, journal} = originalAdvance(false), position = journal.position!, before = storage.getItem(nativeJournalKey(scope));
    const original = prepareNativeCancel(storage, scope, journal, readNativeCancelJournal(storage, scope, position), uncertainLoop);
    const refreshed = readNativeCancelJournal(storage, scope, position);
    expect(prepareNativeCancel(storage, scope, readNativeJournal(storage, scope), refreshed, cancelledUnknown)).toEqual(original);
    expect(() => prepareNativeMutation(storage, scope, journal, "advance", loop)).toThrow("停止请求");
    expect(() => acknowledgeNativeMutation(storage, scope, journal)).toThrow("原推进pending");
    const ack = acknowledgeNativeCancel(storage, scope, position, refreshed, cancelledUnknown);
    expect(ack.pending?.body.idempotency_key).toBe(original.pending?.body.idempotency_key);
    expect(storage.getItem(nativeJournalKey(scope))).toBe(before);
    expect(() => acknowledgeNativeCancel(storage, scope, position, original, cancelledUnknown)).toThrow("另一标签页");
    expect(acknowledgeNativeCancel(storage, scope, position, ack, cancelledUnknown)).toEqual(ack);
    expect(storage.getItem(nativeJournalKey(scope))).toBe(before);
  });
  it("unknown cancellation outcome cannot acknowledge stop or change the original pending", () => {
    const {storage, journal} = originalAdvance(false), position = journal.position!, before = storage.getItem(nativeJournalKey(scope));
    const stop = prepareNativeCancel(storage, scope, journal, readNativeCancelJournal(storage, scope, position), uncertainLoop);
    expect(() => acknowledgeNativeCancel(storage, scope, position, stop, uncertainLoop)).toThrow("尚未确认");
    expect(readNativeCancelJournal(storage, scope, position)).toEqual(stop);
    expect(storage.getItem(nativeJournalKey(scope))).toBe(before);
  });
  it("explicit stop rejection keeps the same stop key and original advance pending; no replacement request is made", () => {
    const {storage, journal} = originalAdvance(true), position = journal.position!, before = storage.getItem(nativeJournalKey(scope));
    const stop = prepareNativeCancel(storage, scope, journal, readNativeCancelJournal(storage, scope, position), uncertainLoop);
    const rejected = rejectNativeCancel(storage, scope, position, stop);
    expect(rejected.pending?.state).toBe("rejected"); expect(rejected.pending?.body).toEqual(stop.pending?.body);
    expect(() => prepareNativeCancel(storage, scope, journal, rejected, uncertainLoop)).toThrow("明确拒绝");
    expect(() => acknowledgeNativeCancel(storage, scope, position, rejected, cancelledUnknown)).toThrow("尚未确认");
    expect(readNativeCancelJournal(storage, scope, position)).toEqual(rejected);
    expect(storage.getItem(nativeJournalKey(scope))).toBe(before);
  });
  it("fails closed on storage fault, cancel-version drift or stale-tab stop writes while retaining all original records", () => {
    const {storage, journal} = originalAdvance(false), position = journal.position!, before = storage.getItem(nativeJournalKey(scope));
    const empty = readNativeCancelJournal(storage, scope, position), denied = {...storage, setItem: () => {throw new Error("quota denied");}};
    expect(() => prepareNativeCancel(denied, scope, journal, empty, uncertainLoop)).toThrow("quota denied");
    expect(readNativeCancelJournal(storage, scope, position)).toEqual(empty); expect(storage.getItem(nativeJournalKey(scope))).toBe(before);
    const stop = prepareNativeCancel(storage, scope, journal, empty, uncertainLoop);
    expect(() => prepareNativeCancel(storage, scope, journal, empty, uncertainLoop)).toThrow("另一标签页");
    expect(() => acknowledgeNativeCancel(denied, scope, position, stop, cancelledUnknown)).toThrow("quota denied");
    expect(readNativeCancelJournal(storage, scope, position)).toEqual(stop);
    storage.setItem(nativeCancelJournalKey(scope, position), JSON.stringify({...stop, version: 2}));
    expect(() => readNativeCancelJournal(storage, scope, position)).toThrow("版本或身份");
    expect(() => prepareNativeMutation(storage, scope, journal, "advance", loop)).toThrow("版本或身份");
    expect(storage.getItem(nativeJournalKey(scope))).toBe(before);
  });
  it("reads a legacy v1 cancel pending with its original key without rewriting the old v1 envelope", () => {
    const {storage, journal} = attached();
    const legacy = prepareNativeMutation(storage, scope, journal, "cancel", loop), position = legacy.position!, before = storage.getItem(nativeJournalKey(scope));
    const stop = prepareNativeCancel(storage, scope, legacy, readNativeCancelJournal(storage, scope, position), uncertainLoop);
    expect(stop.pending?.body.idempotency_key).toBe(legacy.pending?.body.idempotency_key);
    acknowledgeNativeCancel(storage, scope, position, stop, cancelledUnknown);
    expect(storage.getItem(nativeJournalKey(scope))).toBe(before); expect(readNativeJournal(storage, scope)).toEqual(legacy);
  });
});
