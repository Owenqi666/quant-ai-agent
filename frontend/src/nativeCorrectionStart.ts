/** Native entry recovery only. The host owns the immutable plan and original Loop. */
import type { LoopActionViewV2, PublicResearchLoop, PublicResearchLoopV2 } from "./generated/api-contract";
import type { CorrectionScope } from "./researchCorrectionLoop";
import { CORRECTION_BASE, CORRECTION_POLICY, correctionCanAdvance } from "./researchCorrectionLoop";
import { translate } from "./i18n";
import { writeDurably } from "./reviewRecovery";

export const NATIVE_CORRECTION_BASE = "/research-v2/native-correction";
export type NativeCorrectionIdentity = {schema_version: 1; research_id: string; base_revision_id: string; provider_config_id: string};
export type NativeCorrectionAuthorization = {authority_id: "research_loop_native_v1"; grant_id: string;
  kind: "fixture" | "bounded_qualification"; human_authorization_digest: string | null; deadline_at: string;
  max_provider_dispatches: 2; max_repairs: 1; candidate_limit: 1};
export type NativeCorrectionEvidence = {profile_digest: string; profile_status: "pinned_static";
  boundary_status: "fixture_only" | "per_attempt_verification_required";
  inference_status: "unverified" | "fixture_observed" | "native_observed"; semantic_fidelity: "unverified"};
export type NativeCorrectionStartSummary = NativeCorrectionIdentity & {start_id: string; start_digest: string;
  provider_config_digest: string; source_digest: string; session_id: string; loop_id: string; loop_request_digest: string;
  correction_policy_id: typeof CORRECTION_POLICY; correction_policy_digest: string;
  state: "prepared" | "active" | "unknown" | "terminal"; available: boolean; reason_code: string; message: string;
  authorization: NativeCorrectionAuthorization; evidence: NativeCorrectionEvidence;
  development_scope: "ordinary_synthetic_task1_generation1_eligible_repair1"; public_projection_redacted: boolean};
export type NativeCorrectionSupport = NativeCorrectionIdentity & {available: boolean; reason_code: string; message: string;
  plans: NativeCorrectionStartSummary[]; public_projection_redacted: boolean};
export type NativeCorrectionStartRequest = NativeCorrectionIdentity & {start_id: string; start_digest: string; idempotency_key: string};
export type NativeCorrectionStartResponse = {schema_version: 1; start_id: string; start_digest: string; idempotency_key: string;
  attached_existing: true; plan: NativeCorrectionStartSummary; loop: PublicResearchLoopV2};
export type NativePosition = CorrectionScope & {version: 1; plan: NativeCorrectionStartSummary; attached: boolean};
export type NativePending = {operation: "start" | "advance" | "cancel"; path: string;
  body: NativeCorrectionStartRequest | {idempotency_key: string}; state: "unconfirmed" | "rejected"};
export type NativeJournal = {version: 1; scope: string; position: NativePosition | null; pending: NativePending | null};
/** Separate bounded stop receipt. The original v1 mutation remains untouched. */
export type NativeCancelJournal = {version: 1; scope: string; start_id: string; start_digest: string; session_id: string; loop_id: string;
  pending: {path: string; body: {idempotency_key: string}; state: "unconfirmed" | "rejected"} | null; acknowledged: boolean};
type ReadStorage = Pick<Storage, "getItem">;
type WriteStorage = Pick<Storage, "getItem" | "setItem">;
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const closed = (v: Record<string, unknown>, keys: string[]) => Object.keys(v).length === keys.length && keys.every(k => k in v);
const text = (v: unknown): v is string => typeof v === "string";
const identity = (v: unknown): v is string => text(v) && v.length > 0 && v.length <= 128 && /^\S(?:[^\r\n]*\S)?$/.test(v);
const sha = (v: unknown): v is string => text(v) && /^[a-f0-9]{64}$/.test(v);
const token = (v: unknown): v is string => text(v) && /^[A-Za-z0-9_.-]{1,128}$/.test(v);
const idKeys = ["schema_version", "research_id", "base_revision_id", "provider_config_id"];
const planKeys = [...idKeys, "start_id", "start_digest", "provider_config_digest", "source_digest", "session_id", "loop_id",
  "loop_request_digest", "correction_policy_id", "correction_policy_digest", "state", "available", "reason_code", "message",
  "authorization", "evidence", "development_scope", "public_projection_redacted"];
function fail(zh: string, en: string): never {throw new Error(translate(zh, en));}
function validIdentity(v: Record<string, unknown>): boolean {
  return v.schema_version === 1 && identity(v.research_id) && identity(v.base_revision_id) && identity(v.provider_config_id);
}
function validPlan(v: unknown): v is NativeCorrectionStartSummary {
  if (!object(v) || !closed(v, planKeys) || !validIdentity(v) || !text(v.start_id) || !/^native_start_[a-f0-9]{64}$/.test(v.start_id) ||
      ![v.start_digest, v.provider_config_digest, v.source_digest, v.loop_request_digest, v.correction_policy_digest].every(sha) ||
      !identity(v.session_id) || !text(v.loop_id) || !/^research_loop_[a-f0-9]{64}$/.test(v.loop_id) ||
      v.correction_policy_id !== CORRECTION_POLICY || !["prepared", "active", "unknown", "terminal"].includes(String(v.state)) ||
      typeof v.available !== "boolean" || !text(v.reason_code) || !text(v.message) ||
      v.development_scope !== "ordinary_synthetic_task1_generation1_eligible_repair1" || typeof v.public_projection_redacted !== "boolean") return false;
  const a = v.authorization, e = v.evidence;
  return object(a) && closed(a, ["authority_id", "grant_id", "kind", "human_authorization_digest", "deadline_at",
    "max_provider_dispatches", "max_repairs", "candidate_limit"]) && a.authority_id === "research_loop_native_v1" &&
    text(a.grant_id) && /^loop_native_grant_[a-f0-9]{64}$/.test(a.grant_id) && ["fixture", "bounded_qualification"].includes(String(a.kind)) &&
    (a.human_authorization_digest === null || sha(a.human_authorization_digest)) && text(a.deadline_at) && Number.isFinite(Date.parse(a.deadline_at)) &&
    a.max_provider_dispatches === 2 && a.max_repairs === 1 && a.candidate_limit === 1 &&
    object(e) && closed(e, ["profile_digest", "profile_status", "boundary_status", "inference_status", "semantic_fidelity"]) &&
    sha(e.profile_digest) && e.profile_status === "pinned_static" && ["fixture_only", "per_attempt_verification_required"].includes(String(e.boundary_status)) &&
    ["unverified", "fixture_observed", "native_observed"].includes(String(e.inference_status)) && e.semantic_fidelity === "unverified";
}
export function nativeIdentity(scope: CorrectionScope, baseRevisionId: string): NativeCorrectionIdentity {
  const v: NativeCorrectionIdentity = {schema_version: 1, research_id: scope.researchId,
    base_revision_id: baseRevisionId, provider_config_id: scope.providerConfigId};
  if (!validIdentity(v)) fail("Native任务身份尚未完整，不能领取计划。", "The Native task identity is incomplete. No plan can be attached.");
  return v;
}
export function requireNativeSupport(value: unknown, expected: NativeCorrectionIdentity): NativeCorrectionSupport {
  if (!object(value) || !closed(value, [...idKeys, "available", "reason_code", "message", "plans", "public_projection_redacted"]) ||
      !validIdentity(value) || idKeys.some(k => value[k] !== expected[k as keyof NativeCorrectionIdentity]) ||
      typeof value.available !== "boolean" || !text(value.reason_code) || !text(value.message) || typeof value.public_projection_redacted !== "boolean" ||
      !Array.isArray(value.plans) || value.plans.length > 32 || !value.plans.every(p => validPlan(p) && idKeys.every(k => p[k as keyof NativeCorrectionIdentity] === expected[k as keyof NativeCorrectionIdentity])) ||
      new Set(value.plans.map(p => p.start_id)).size !== value.plans.length)
    fail("Native支持响应与冻结身份或版本不匹配；没有替换原计划。", "Native support does not match the frozen identity or version. The original plan was retained.");
  return value as NativeCorrectionSupport;
}
/** Mutable host state is displayed separately; it cannot rewrite frozen pins. */
export function sameNativePlan(a: NativeCorrectionStartSummary, b: NativeCorrectionStartSummary): boolean {
  const mutable = new Set(["state", "available", "reason_code", "message", "evidence", "public_projection_redacted"]);
  return planKeys.filter(k => !mutable.has(k)).every(k => JSON.stringify(a[k as keyof NativeCorrectionStartSummary]) === JSON.stringify(b[k as keyof NativeCorrectionStartSummary])) &&
    a.evidence.profile_digest === b.evidence.profile_digest && a.evidence.profile_status === b.evidence.profile_status &&
    a.evidence.boundary_status === b.evidence.boundary_status && a.evidence.semantic_fidelity === b.evidence.semantic_fidelity;
}
export const nativeJournalKey = (scope: CorrectionScope) =>
  `paper-alpha:native-correction:v1:${JSON.stringify([scope.workspaceId, scope.researchId, scope.providerConfigId])}`;
export const nativeLockName = (scope: CorrectionScope) => nativeJournalKey(scope);
const emptyJournal = (scope: CorrectionScope): NativeJournal => ({version: 1, scope: nativeJournalKey(scope), position: null, pending: null});
function requirePending(p: unknown, position: NativePosition): p is NativePending {
  if (!object(p) || !closed(p, ["operation", "path", "body", "state"]) || !["unconfirmed", "rejected"].includes(String(p.state)) || !object(p.body) || !token(p.body.idempotency_key)) return false;
  const body = p.body;
  if (p.operation === "start") return p.path === `${NATIVE_CORRECTION_BASE}/start` &&
    closed(body, [...idKeys, "start_id", "start_digest", "idempotency_key"]) &&
    idKeys.every(k => body[k] === position.plan[k as keyof NativeCorrectionIdentity]) &&
    p.body.start_id === position.plan.start_id && p.body.start_digest === position.plan.start_digest;
  return ["advance", "cancel"].includes(String(p.operation)) && closed(p.body, ["idempotency_key"]) &&
    p.path === `${CORRECTION_BASE}/${encodeURIComponent(position.plan.loop_id)}/${p.operation}` && position.attached;
}
export function readNativeJournal(storage: ReadStorage, scope: CorrectionScope): NativeJournal {
  const key = nativeJournalKey(scope), raw = storage.getItem(key);
  if (raw === null) return emptyJournal(scope);
  try {
    const v: unknown = JSON.parse(raw);
    if (!object(v) || !closed(v, ["version", "scope", "position", "pending"]) || v.version !== 1 || v.scope !== key) throw new Error();
    const p = v.position;
    if (p === null) {if (v.pending !== null) throw new Error();}
    else if (!object(p) || !closed(p, ["version", "workspaceId", "researchId", "providerConfigId", "plan", "attached"]) || p.version !== 1 ||
      p.workspaceId !== scope.workspaceId || p.researchId !== scope.researchId || p.providerConfigId !== scope.providerConfigId || typeof p.attached !== "boolean" ||
      !validPlan(p.plan) || p.plan.research_id !== scope.researchId || p.plan.provider_config_id !== scope.providerConfigId ||
      v.pending !== null && !requirePending(v.pending, p as unknown as NativePosition)) throw new Error();
    return v as unknown as NativeJournal;
  } catch {return fail("已保存的Native计划或请求版本无法核实；保留记录，停止新操作。", "The saved Native plan or request version could not be verified. Its record is retained; new actions are blocked.");}
}
function writeJournal(storage: WriteStorage, scope: CorrectionScope, expected: NativeJournal, next: NativeJournal): NativeJournal {
  if (JSON.stringify(readNativeJournal(storage, scope)) !== JSON.stringify(expected))
    fail("另一标签页已改变Native恢复记录；请只读刷新原任务。", "Another tab changed the Native recovery record. Refresh the original task read only.");
  writeDurably(storage, nativeJournalKey(scope), next);
  return readNativeJournal(storage, scope);
}
export function pinNativePlan(storage: WriteStorage, scope: CorrectionScope, expected: NativeJournal, plan: NativeCorrectionStartSummary): NativeJournal {
  if (!validPlan(plan) || plan.research_id !== scope.researchId || plan.provider_config_id !== scope.providerConfigId)
    fail("宿主计划的身份、范围或两槽预算无法核实。", "The host plan identity, scope or two-slot budget could not be verified.");
  if (expected.position) {
    if (!sameNativePlan(expected.position.plan, plan)) fail("已冻结另一Native计划；未知或失败不会替换为新计划。", "A different Native plan is pinned. Unknown or failed tasks are never replaced with a new plan.");
    return writeJournal(storage, scope, expected, expected);
  }
  if (!plan.available || ["unknown", "terminal"].includes(plan.state))
    fail("此原计划已被阻断或结果未知；不能领取替代任务。", "This original plan is blocked or unknown. No substitute task can be attached.");
  return writeJournal(storage, scope, expected, {...expected, position: {...scope, version: 1, plan, attached: false}});
}
export function prepareNativeMutation(storage: WriteStorage, scope: CorrectionScope, expected: NativeJournal,
  operation: NativePending["operation"], loop: PublicResearchLoopV2 | null): NativeJournal {
  const p = expected.position;
  if (!p) fail("先选择宿主批准计划。", "Select a host-approved plan first.");
  if (operation !== "cancel" && p.attached) {
    const stop = readNativeCancelJournal(storage, scope, p);
    if (stop.pending || stop.acknowledged) fail("原Native任务已有停止请求；保留原步骤，不再派发推进。", "The original Native task has a stop request. Its original step is retained and cannot be advanced.");
  }
  if (expected.pending) {
    if (expected.pending.operation !== operation || expected.pending.state === "rejected") fail("原Native请求尚未解除；不创建替代请求。", "The original Native request remains pinned. No substitute request is created.");
    return writeJournal(storage, scope, expected, expected);
  }
  if (operation === "start" && p.attached || operation !== "start" && !p.attached)
    fail("Native任务连接状态不允许此操作。", "The Native attachment state does not allow this action.");
  if (operation !== "start") {
    if (!loop) fail("原循环尚未核实；先只读刷新。", "The original loop has not been verified. Refresh read only first.");
    requireNativeLoop(loop, p);
    if (operation === "advance" && !correctionCanAdvance(loop)) fail("原Native任务没有可推进步骤。", "The original Native task has no step to advance.");
    if (operation === "cancel" && !nativeCanCancel(loop, p))
      fail("原任务已经停止，取消不会创建新步骤。", "The original task is already stopped; cancellation does not create a new step.");
  }
  const originalAction = loop?.actions.find(a => a.status === "pending");
  // Stable keys make simultaneous tabs submit the same host step, even if storage locks are unavailable.
  const key = operation === "advance" && originalAction ? originalAction.idempotency_key :
    `native-${operation}-${p.plan.start_digest}-${operation === "start" ? 0 : operation === "cancel" ? 0 : loop!.actions.length + 1}`;
  const pending: NativePending = {operation, path: operation === "start" ? `${NATIVE_CORRECTION_BASE}/start` :
    `${CORRECTION_BASE}/${encodeURIComponent(p.plan.loop_id)}/${operation}`, state: "unconfirmed",
    body: operation === "start" ? {...nativeIdentity(scope, p.plan.base_revision_id), start_id: p.plan.start_id,
      start_digest: p.plan.start_digest, idempotency_key: key} : {idempotency_key: key}};
  return writeJournal(storage, scope, expected, {...expected, pending});
}
export function requireNativeLoop(value: PublicResearchLoop | PublicResearchLoopV2, p: NativePosition): PublicResearchLoopV2 {
  const plan = p.plan;
  if (value.schema_version !== 2 || value.id !== plan.loop_id || value.request_digest !== plan.loop_request_digest ||
      value.session_id !== plan.session_id || value.session.id !== plan.session_id || value.session.config.provider_config_id !== plan.provider_config_id ||
      value.session.config.provider_config_digest !== plan.provider_config_digest || value.session.config.contract_id !== "research_provider_v2" ||
      value.session.root_source.kind !== "research_revision" || value.session.root_source.research_id !== plan.research_id ||
      value.session.root_source.revision_id !== plan.base_revision_id || value.session.root_source.source_digest !== plan.source_digest ||
      value.policy.correction_policy_id !== plan.correction_policy_id || value.policy.correction_policy_digest !== plan.correction_policy_digest ||
      value.policy.correction_mode !== "provider_optional" || value.policy.max_repairs !== 1 || value.session.budget.max_provider_dispatches !== 2 ||
      value.semantic_fidelity !== "unverified" || value.session.usage.provider_dispatches > 2)
    fail("返回的Native循环与原计划或两槽预算不一致；未采用响应。", "The Native loop does not match the original plan or two-slot budget. This response was not applied.");
  return value;
}
export function requireNativeStart(value: NativeCorrectionStartResponse, journal: NativeJournal): PublicResearchLoopV2 {
  const p = journal.position, pending = journal.pending;
  if (!p || pending?.operation !== "start" || !object(value) ||
      !closed(value, ["schema_version", "start_id", "start_digest", "idempotency_key", "attached_existing", "plan", "loop"]) ||
      value.schema_version !== 1 || value.attached_existing !== true || value.start_id !== p.plan.start_id || value.start_digest !== p.plan.start_digest ||
      value.idempotency_key !== pending.body.idempotency_key || !validPlan(value.plan) || !sameNativePlan(value.plan, p.plan))
    fail("Native连接响应未确认原计划与原请求；保持待确认。", "The Native attachment did not confirm the original plan and request. The request remains unconfirmed.");
  return requireNativeLoop(value.loop, p);
}
export function acknowledgeNativeMutation(storage: WriteStorage, scope: CorrectionScope, original: NativeJournal): NativeJournal {
  if (!original.pending || !original.position) fail("没有可确认的Native原请求。", "There is no original Native request to acknowledge.");
  if (original.position.attached && original.pending.operation === "advance") {
    const stop = readNativeCancelJournal(storage, scope, original.position);
    if (stop.pending || stop.acknowledged) fail("停止请求已登记；原推进pending及未知状态完整保留。", "The stop request is recorded. The original advance pending and unknown state remain intact.");
  }
  return writeJournal(storage, scope, original, {...original, position: {...original.position,
    attached: original.position.attached || original.pending.operation === "start"}, pending: null});
}
export function rejectNativeMutation(storage: WriteStorage, scope: CorrectionScope, original: NativeJournal): NativeJournal {
  if (!original.pending) return original;
  return writeJournal(storage, scope, original, {...original, pending: {...original.pending, state: "rejected"}});
}
export const nativeCancelJournalKey = (scope: CorrectionScope, position: NativePosition) =>
  `paper-alpha:native-correction-cancel:v1:${JSON.stringify([scope.workspaceId, scope.researchId, scope.providerConfigId, position.plan.start_id])}`;
export function nativeCanCancel(loop: PublicResearchLoopV2, position: NativePosition): boolean {
  requireNativeLoop(loop, position);
  return position.attached && !["completed", "stopped", "cancelled", "exhausted", "interrupted"].includes(loop.session.effective_state);
}
export function readNativeCancelJournal(storage: ReadStorage, scope: CorrectionScope, position: NativePosition): NativeCancelJournal {
  const key = nativeCancelJournalKey(scope, position), plan = position.plan;
  const empty: NativeCancelJournal = {version: 1, scope: key, start_id: plan.start_id, start_digest: plan.start_digest,
    session_id: plan.session_id, loop_id: plan.loop_id, pending: null, acknowledged: false};
  const raw = storage.getItem(key);
  if (raw === null) return empty;
  try {
    const v: unknown = JSON.parse(raw);
    if (!object(v) || !closed(v, ["version", "scope", "start_id", "start_digest", "session_id", "loop_id", "pending", "acknowledged"]) ||
      v.version !== 1 || v.scope !== key || v.start_id !== plan.start_id || v.start_digest !== plan.start_digest ||
      v.session_id !== plan.session_id || v.loop_id !== plan.loop_id || typeof v.acknowledged !== "boolean") throw new Error();
    const p = v.pending;
    if (p === null) {if (v.acknowledged) throw new Error();}
    else if (!object(p) || !closed(p, ["path", "body", "state"]) || p.path !== `${CORRECTION_BASE}/${encodeURIComponent(plan.loop_id)}/cancel` ||
      !["unconfirmed", "rejected"].includes(String(p.state)) || !object(p.body) || !closed(p.body, ["idempotency_key"]) ||
      !token(p.body.idempotency_key) || v.acknowledged && p.state !== "unconfirmed") throw new Error();
    return v as NativeCancelJournal;
  } catch {return fail("原Native停止请求的版本或身份无法核实；保留全部记录，停止新操作。", "The original Native stop request version or identity could not be verified. All records are retained and new actions are blocked.");}
}
function writeCancelJournal(storage: WriteStorage, scope: CorrectionScope, position: NativePosition,
  expected: NativeCancelJournal, next: NativeCancelJournal): NativeCancelJournal {
  if (JSON.stringify(readNativeCancelJournal(storage, scope, position)) !== JSON.stringify(expected))
    fail("另一标签页已改变原Native停止请求；沿用已保存请求核对。", "Another tab changed the original Native stop request. Reconcile the saved request.");
  writeDurably(storage, nativeCancelJournalKey(scope, position), next);
  return readNativeCancelJournal(storage, scope, position);
}
export function prepareNativeCancel(storage: WriteStorage, scope: CorrectionScope, original: NativeJournal,
  expected: NativeCancelJournal, loop: PublicResearchLoopV2 | null): NativeCancelJournal {
  const position = original.position;
  if (!position?.attached || !loop) fail("尚未核实已连接的原Native循环，不能停止。", "The attached original Native loop has not been verified. It cannot be stopped yet.");
  requireNativeLoop(loop, position);
  if (JSON.stringify(readNativeJournal(storage, scope)) !== JSON.stringify(original))
    fail("原Native恢复记录已改变；没有覆盖原pending，请先只读刷新。", "The original Native recovery record changed. Its pending was not overwritten; refresh read only first.");
  if (expected.pending) {
    if (expected.pending.state === "rejected") fail("原停止请求已明确拒绝，全部记录保留供宿主检查。", "The original stop request was explicitly rejected. All records are retained for host inspection.");
    return writeCancelJournal(storage, scope, position, expected, expected);
  }
  if (!nativeCanCancel(loop, position)) fail("原Session已停止或完成；没有创建新的停止请求。", "The original session has stopped or completed. No new stop request was created.");
  const legacy = original.pending?.operation === "cancel" ? original.pending : null;
  if (legacy?.state === "rejected") fail("原停止请求已明确拒绝，全部记录保留供宿主检查。", "The original stop request was explicitly rejected. All records are retained for host inspection.");
  return writeCancelJournal(storage, scope, position, expected, {...expected, pending: {path: `${CORRECTION_BASE}/${encodeURIComponent(position.plan.loop_id)}/cancel`,
    body: {idempotency_key: legacy?.body.idempotency_key || `native-cancel-${position.plan.start_digest}-0`}, state: "unconfirmed"}});
}
export function acknowledgeNativeCancel(storage: WriteStorage, scope: CorrectionScope, position: NativePosition,
  original: NativeCancelJournal, loop: PublicResearchLoopV2): NativeCancelJournal {
  requireNativeLoop(loop, position);
  if (!original.pending || original.pending.state !== "unconfirmed" || !["completed", "stopped", "cancelled", "exhausted", "interrupted"].includes(loop.session.effective_state))
    fail("原Session停止结果尚未确认；停止请求与原pending均保留。", "The original session stop is not confirmed. The stop request and original pending are retained.");
  return writeCancelJournal(storage, scope, position, original, {...original, acknowledged: true});
}
export function rejectNativeCancel(storage: WriteStorage, scope: CorrectionScope, position: NativePosition,
  original: NativeCancelJournal): NativeCancelJournal {
  if (!original.pending || original.acknowledged) return original;
  return writeCancelJournal(storage, scope, position, original, {...original, pending: {...original.pending, state: "rejected"}});
}
export function nativeReceiptLinks(loop: PublicResearchLoopV2) {
  const intents = loop.actions.filter(a => a.provider_correction), receipts = loop.actions.filter(a => a.provider_correction_receipt);
  function bound(create: LoopActionViewV2, reconcile: LoopActionViewV2): boolean {
    const intent = create.provider_correction!, receipt = reconcile.provider_correction_receipt!, association = receipt.provider_association;
    // The host saves intent on create and receipt on reconcile. Sequence verifies the
    // original actions; it never chooses a pair in place of their immutable bindings.
    if (loop.schema_version !== 2 || loop.policy.correction_mode !== "provider_optional" || loop.session_id !== loop.session.id ||
        create.kind !== "proposal.create" || create.status !== "acknowledged" || create.proposal_id !== intent.proposal_id ||
        create.request_digest !== intent.intent_digest || reconcile.kind !== "proposal.reconcile" || reconcile.status !== "acknowledged" ||
        !Number.isInteger(create.sequence) || !Number.isInteger(reconcile.sequence) || create.sequence < 1 ||
        reconcile.sequence <= create.sequence || reconcile.sequence > 24 || receipt.created_at !== reconcile.planned_at ||
        intent.schema_version !== 1 || receipt.schema_version !== 1 || receipt.method !== "provider" || !association ||
        intent.policy_id !== loop.policy.correction_policy_id || intent.policy_digest !== loop.policy.correction_policy_digest ||
        receipt.policy_id !== intent.policy_id || receipt.policy_digest !== intent.policy_digest ||
        receipt.parent_binding_digest !== intent.parent_binding_digest || receipt.source_digest !== intent.source_digest ||
        receipt.diagnostics_digest !== intent.diagnostics_digest || receipt.candidate_id !== intent.parent.candidate_id ||
        !intent.proposal_id || association.proposal_id !== intent.proposal_id || intent.parent.proposal_id === intent.proposal_id ||
        intent.source_digest !== loop.session.root_source.source_digest) return false;
    const parents = loop.proposals.filter(p => p.id === intent.parent.proposal_id), children = loop.proposals.filter(p => p.id === intent.proposal_id);
    if (parents.length !== 1 || children.length !== 1) return false;
    const parent = parents[0], child = children[0];
    if ([parent, child].some(p => p.session_id !== loop.session_id || p.provider_config_id !== loop.session.config.provider_config_id ||
        p.source_digest !== intent.source_digest || p.source_kind !== "research_revision") ||
        parent.operation !== "generate_candidates" || parent.status !== "rejected" || intent.parent.status !== "rejected" ||
        parent.current_attempt_id !== intent.parent.attempt_id || parent.accepted_attempt_id !== null || parent.accepted_response_digest !== null ||
        child.operation !== "repair_candidate" || child.status !== intent.proposal_status || child.current_attempt_id !== association.attempt_id) return false;
    // Public summaries omit the rejected parent's response. Its original finished
    // reservation exposes that digest; no private ledger field is reconstructed.
    const parentReservations = loop.session.reservations.filter(r => r.children?.proposal_id === parent.id);
    const childReservations = loop.session.reservations.filter(r => r.children?.proposal_id === child.id);
    if (parentReservations.length !== 1 || childReservations.length !== 1) return false;
    const parentReservation = parentReservations[0], childReservation = childReservations[0];
    if (parentReservation.action !== "generate" || parentReservation.status !== "finished" ||
        parentReservation.source.source_digest !== intent.source_digest || parentReservation.outcome?.result_digest !== intent.parent.response_digest ||
        childReservation.action !== "repair" || childReservation.status !== "finished" || childReservation.effect_key !== intent.effect_key ||
        childReservation.action_digest !== intent.intent_digest || childReservation.source.source_digest !== intent.source_digest ||
        childReservation.outcome?.result_digest !== association.response_digest) return false;
    return receipt.status === "completed" ? ["ready", "handing_off", "handed_off"].includes(child.status) &&
      child.accepted_attempt_id === association.attempt_id && child.accepted_response_digest === association.response_digest &&
      !!receipt.accepted_task_digest && receipt.accepted_expression !== null : receipt.status === "blocked" && child.status === "rejected" &&
      child.accepted_attempt_id === null && child.accepted_response_digest === null && receipt.accepted_task_digest === null && receipt.accepted_expression === null;
  }
  const matches = intents.map(intent => receipts.filter(receipt => bound(intent, receipt)));
  const used = new Set<LoopActionViewV2>();
  const links = intents.map((action, index) => {
    const candidates = matches[index], candidate = candidates.length === 1 ? candidates[0] : null;
    // Ambiguous duplicate intents or receipts remain unknown instead of selecting
    // an action by array position, expression, or name.
    const paired = candidate && matches.filter(items => items.includes(candidate)).length === 1 ? candidate : null;
    if (paired) used.add(paired);
    const receipt = paired?.provider_correction_receipt || null;
    return {sequence: action.sequence, intent: action.provider_correction || null, receipt, status: receipt?.status || "unknown" as const};
  });
  for (const action of receipts) if (!used.has(action)) links.push({sequence: action.sequence, intent: null,
    receipt: action.provider_correction_receipt || null, status: "unknown"});
  return links;
}
