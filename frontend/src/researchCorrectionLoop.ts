/** Display/request preparation only. The original host owns source and effect authority. */
import { translate } from "./i18n";
import type {
  LoopCreateV2, LoopSourceSupport, ProvidersResponse, PublicResearchLoop,
  PublicResearchLoopV2, PublicResearchSession, ResearchV2SessionCreate,
} from "./generated/api-contract";
import { readMutation, recoveryKey, stageMutation, writeDurably } from "./reviewRecovery";
import type { PendingMutation } from "./reviewRecovery";

export const CORRECTION_BASE = "/research-v2/loops";
export const CORRECTION_POLICY = "equivalent-expression-representation-v2";
export const CORRECTION_SESSION_BASE = "/research-v2/sessions";
export interface CorrectionScope {
  workspaceId: string; researchId: string; providerConfigId: string;
}
export interface CorrectionStartPlan {
  baseRevisionId: string;
  sourceDigest: string;
  providerConfigDigest: string;
  policyId: typeof CORRECTION_POLICY;
  policyDigest: string;
  allowExperiment: boolean;
}
export interface CorrectionPosition extends CorrectionScope {
  version: 1;
  sessionId: string;
  loopId: string;
  plan: CorrectionStartPlan;
}
const sha = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const identity = (value: unknown): value is string => typeof value === "string" && !!value.trim() && value.length <= 128;
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const closed = (value: Record<string, unknown>, keys: string[]) => keys.length === Object.keys(value).length && keys.every(key => key in value);
function fail(zh: string, en: string): never { throw new Error(translate(zh, en)); }
const validPlan = (value: unknown): value is CorrectionStartPlan => object(value) &&
  closed(value, ["baseRevisionId", "sourceDigest", "providerConfigDigest", "policyId", "policyDigest", "allowExperiment"]) &&
  identity(value.baseRevisionId) && sha(value.sourceDigest) && sha(value.providerConfigDigest) &&
  value.policyId === CORRECTION_POLICY && sha(value.policyDigest) && typeof value.allowExperiment === "boolean";
export function correctionPendingPlan(item: PendingMutation | null, scope: CorrectionScope): CorrectionStartPlan {
  const plan = item?.context?.plan;
  const source = item?.body.source, budget = item?.body.budget;
  if (!item || !validPlan(plan) || !object(source) || source.kind !== "research_revision" || source.research_id !== scope.researchId ||
      source.base_revision_id !== plan.baseRevisionId || item?.body.provider_config_id !== scope.providerConfigId ||
      item.body.expected_source_digest !== plan.sourceDigest || item.body.expected_provider_config_digest !== plan.providerConfigDigest ||
      !object(budget) || budget.max_provider_dispatches !== 1 || budget.max_tool_actions !== (plan.allowExperiment ? 3 : 2) ||
      budget.max_experiments !== (plan.allowExperiment ? 1 : 0) || budget.max_failures !== 3 || budget.max_seconds !== 600)
    return fail("原请求的冻结计划无法核实，没有采用当前选择或新预算。", "The original frozen plan could not be verified. Current choices or a new budget were not substituted.");
  return plan;
}

export const correctionPositionKey = (scope: CorrectionScope) => recoveryKey(scope.workspaceId, "correction-position", scope.researchId, scope.providerConfigId);
export const correctionMutationKey = (scope: CorrectionScope, operation: string, target = "") =>
  recoveryKey(scope.workspaceId, "correction-" + operation, scope.researchId, scope.providerConfigId, target);

export function readCorrectionPosition(storage: Pick<Storage, "getItem">, scope: CorrectionScope): CorrectionPosition | null {
  const raw = storage.getItem(correctionPositionKey(scope));
  if (raw === null) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!object(value) || !closed(value, ["version", "workspaceId", "researchId", "providerConfigId", "sessionId", "loopId", "plan"]) || value.version !== 1 ||
        value.workspaceId !== scope.workspaceId || value.researchId !== scope.researchId || value.providerConfigId !== scope.providerConfigId ||
        !identity(value.sessionId) || typeof value.loopId !== "string" || value.loopId !== "" && !identity(value.loopId) || !validPlan(value.plan)) throw new Error();
    return value as unknown as CorrectionPosition;
  } catch {
    return fail("已保存的修正任务位置无法核实；为避免重复创建，请保留浏览器记录并检查。", "The saved correction position could not be verified. Keep the browser record and inspect it before creating another task.");
  }
}
export function saveCorrectionPosition(storage: Pick<Storage, "getItem" | "setItem">, value: CorrectionPosition) {
  const previous = readCorrectionPosition(storage, value);
  if (previous && (previous.sessionId !== value.sessionId || JSON.stringify(previous.plan) !== JSON.stringify(value.plan) || previous.loopId && previous.loopId !== value.loopId))
    fail("另一项任务已改变本机位置；原请求保留，请刷新后继续。", "Another task changed the saved position. The original request is retained; refresh before continuing.");
  writeDurably(storage, correctionPositionKey(value), value);
}
export function clearCorrectionPosition(storage: Pick<Storage, "getItem" | "removeItem">, expected: CorrectionPosition) {
  const previous = readCorrectionPosition(storage, expected);
  if (JSON.stringify(previous) !== JSON.stringify(expected))
    fail("本机任务位置已改变，没有清除其他任务。", "The saved position changed. No other task was cleared.");
  const key = correctionPositionKey(expected);
  storage.removeItem(key);
  if (storage.getItem(key) !== null) fail("无法确认任务位置已清除。", "The saved position could not be cleared.");
}

export function correctionStartPlan(researchId: string, baseRevisionId: string, providerConfigId: string,
  support: LoopSourceSupport, providers: ProvidersResponse, allowExperiment: boolean): CorrectionStartPlan {
  if (!identity(researchId) || !identity(baseRevisionId) || support.research_id !== researchId || support.base_revision_id !== baseRevisionId)
    fail("来源支持信息与当前研究修订不匹配，请重新核对。", "Source support does not match this research revision. Check it again.");
  const config = providers.providers.find(item => item.config_id === providerConfigId);
  if (!config?.dispatchable || config.mode !== "fixture" || !providers.allow_fixture || providers.dispatch_paused)
    fail("此入口仅使用宿主显式启用的合成示例 Provider。", "This entry requires an explicitly enabled synthetic fixture provider.");
  if (!support.correction_supported || support.support_status !== "supported" || !support.generation_available ||
      support.accepted_source_kind !== "research_revision" || support.accepted_data_kind !== "synthetic" ||
      support.accepted_task_schema_version !== 1 || support.candidate_limit !== 1 || support.max_deterministic_passes !== 1 ||
      !support.correction_modes.includes("deterministic") || !sha(support.source_digest) || !sha(support.provider_config_digest))
    fail("当前来源或生成方式尚未获得此版本支持，请查看阻断原因。", "This source or generation path is unsupported. Inspect the blocker.");
  const references = support.policy_references.filter(item => item.policy_id === CORRECTION_POLICY);
  if (references.length !== 1 || !support.policy_ids.includes(CORRECTION_POLICY) || !sha(references[0].policy_digest))
    fail("宿主策略引用缺失或不一致，不能创建修正任务。", "The host policy reference is missing or inconsistent. A correction task cannot be created.");
  return { baseRevisionId, sourceDigest: support.source_digest, providerConfigDigest: support.provider_config_digest,
    policyId: CORRECTION_POLICY, policyDigest: references[0].policy_digest, allowExperiment };
}
export function correctionSessionRequest(scope: CorrectionScope, plan: CorrectionStartPlan): Omit<ResearchV2SessionCreate, "idempotency_key"> {
  return { source: {kind: "research_revision", research_id: scope.researchId, base_revision_id: plan.baseRevisionId},
    host_grant_id: null, provider_config_id: scope.providerConfigId, expected_source_digest: plan.sourceDigest,
    expected_provider_config_digest: plan.providerConfigDigest,
    purpose: "Explicit synthetic Alpha#006 workflow; deterministic representation correction; research quality remains unverified.",
    budget: {max_provider_dispatches: 1, max_tool_actions: plan.allowExperiment ? 3 : 2,
      max_experiments: plan.allowExperiment ? 1 : 0, max_failures: 3, max_seconds: 600} };
}
export function requireCorrectionSession(session: PublicResearchSession, scope: CorrectionScope, position: CorrectionPosition) {
  if (session.id !== position.sessionId || session.config.provider_config_id !== scope.providerConfigId ||
      session.config.provider_config_digest !== position.plan.providerConfigDigest || session.config.contract_id !== "research_provider_v2" ||
      session.root_source.kind !== "research_revision" || session.root_source.research_id !== scope.researchId ||
      session.root_source.revision_id !== position.plan.baseRevisionId || session.root_source.source_digest !== position.plan.sourceDigest)
    fail("会话身份与冻结任务位置不一致，未采用此响应。", "The session does not match the frozen task position. This response was not applied.");
  return session;
}
export function correctionLoopRequest(session: PublicResearchSession, scope: CorrectionScope,
  position: CorrectionPosition, proposalKey: string): Omit<LoopCreateV2, "idempotency_key"> {
  requireCorrectionSession(session, scope, position);
  if (session.remaining_seconds <= 0 || !["created", "active"].includes(session.effective_state) ||
      session.current_source.source_digest !== position.plan.sourceDigest || session.current_source.revision_id !== position.plan.baseRevisionId ||
      session.usage.provider_dispatches !== 0 || session.usage.tool_actions !== 0 || session.usage.experiments !== 0 || !identity(proposalKey))
    fail("会话已使用、来源已改变或预算已闭合；没有创建新的循环。", "The session was used, its source changed, or its budget closed. No new loop was created.");
  return {schema_version: 2, initial_proposal: {schema_version: 2, contract_id: "research_provider_v2",
    operation: "generate_candidates", session_id: session.id, expected_session_digest: session.state_digest,
    source: {kind: "research_revision", research_id: scope.researchId, base_revision_id: position.plan.baseRevisionId},
    expected_source_digest: position.plan.sourceDigest, provider_config_id: scope.providerConfigId,
    candidate_limit: 1, budget: {max_provider_invocations: 1, max_seconds: 120}, idempotency_key: proposalKey},
    max_repairs: 1, correction_mode: "deterministic", correction_policy_id: position.plan.policyId,
    correction_policy_digest: position.plan.policyDigest, allow_experiment: position.plan.allowExperiment,
    experiment_budget: position.plan.allowExperiment ? {max_steps: 10, max_failures: 2, max_seconds: 180} : null};
}
export function requireCorrectionLoop(value: PublicResearchLoop | PublicResearchLoopV2, scope: CorrectionScope,
  position?: CorrectionPosition): PublicResearchLoopV2 {
  if (value.schema_version !== 2 || value.session_id !== value.session.id || value.session.config.provider_config_id !== scope.providerConfigId ||
      value.session.root_source.kind !== "research_revision" || value.session.root_source.research_id !== scope.researchId ||
      position && (position.loopId && value.id !== position.loopId || value.session_id !== position.sessionId ||
        value.session.config.provider_config_digest !== position.plan.providerConfigDigest ||
        value.session.root_source.revision_id !== position.plan.baseRevisionId || value.session.root_source.source_digest !== position.plan.sourceDigest ||
        value.policy.correction_policy_id !== position.plan.policyId || value.policy.correction_policy_digest !== position.plan.policyDigest ||
        value.policy.correction_mode !== "deterministic" || value.policy.allow_experiment !== position.plan.allowExperiment))
    fail("返回的循环不属于当前修正任务；旧版本不会用新规则重放。", "The loop does not belong to this correction task. Legacy loops are not replayed with new rules.");
  return value;
}
export function correctionPositionFromLoop(value: PublicResearchLoopV2, scope: CorrectionScope): CorrectionPosition {
  requireCorrectionLoop(value, scope);
  const source = value.session.root_source;
  if (!source.revision_id || !sha(source.source_digest) || value.policy.correction_policy_id !== CORRECTION_POLICY)
    fail("此历史任务不属于当前展示策略。", "This historical task does not use the displayed policy.");
  return {...scope, version: 1, sessionId: value.session_id, loopId: value.id,
    plan: {baseRevisionId: source.revision_id, sourceDigest: source.source_digest,
      providerConfigDigest: value.session.config.provider_config_digest, policyId: CORRECTION_POLICY,
      policyDigest: value.policy.correction_policy_digest, allowExperiment: value.policy.allow_experiment}};
}
export function loopPolls(value: PublicResearchLoopV2) {
  return ["waiting_provider", "waiting_execution"].includes(value.state);
}
export function pendingLoopAction(value: PublicResearchLoopV2) {
  return value.actions.find(action => action.status === "pending") || null;
}
export function correctionCanAdvance(value: PublicResearchLoopV2) {
  return !!pendingLoopAction(value) || ["ready", "recovering"].includes(value.state) && value.next_action !== null;
}
export function correctionNextLabel(value: PublicResearchLoopV2) {
  if (pendingLoopAction(value)) return translate("核对原步骤（沿用原请求）", "Reconcile the original step (same request)");
  const labels: Record<string, [string, string]> = {
    "proposal.create": ["生成一个候选", "Generate one candidate"],
    "proposal.reconcile": ["核对实际生成结果", "Check the actual generated result"],
    "correction.apply": ["执行一次等价表示修正", "Apply one equivalent representation correction"],
    "experiment.create": ["创建验证期实验", "Create a validation experiment"],
    "experiment.advance": ["继续验证期实验", "Continue the validation experiment"],
    "experiment.reconcile": ["核对实际实验结果", "Check the actual experiment result"],
    "session.stop": ["保存停止状态", "Record the stopped state"],
  };
  return labels[value.next_action || ""] ? translate(...labels[value.next_action!]) : translate("核对任务状态", "Check task state");
}
/** Pin a server-saved pending action even when the browser lost its journal.
 * A new UI UUID cannot replace the actual ledger key during recovery.
 */
export function prepareCorrectionAdvance(storage: Pick<Storage, "getItem" | "setItem">, key: string,
  path: string, value: PublicResearchLoopV2 | null, freshKey: string, loopId = value?.id || ""): PendingMutation {
  const existing = readMutation(storage, key, path);
  if (existing) {
    if (existing.state === "rejected") fail("原步骤已明确拒绝，需先解除原请求。", "The original step was explicitly rejected. Release its request first.");
    if (!identity(loopId) || existing.context?.loop_id !== loopId || path !== `${CORRECTION_BASE}/${encodeURIComponent(loopId)}/advance`)
      fail("待确认请求不属于当前循环，原记录保留。", "The unconfirmed request does not belong to this loop. Its record is retained.");
    return existing;
  }
  if (!value || value.id !== loopId || path !== `${CORRECTION_BASE}/${encodeURIComponent(loopId)}/advance` || !correctionCanAdvance(value))
    fail("此任务没有可推进的步骤；刷新只读取状态。", "This task has no step to advance. Refresh only reads its state.");
  const pending = pendingLoopAction(value);
  return stageMutation(storage, key, path, {}, pending?.idempotency_key || freshKey,
    {loop_id: value.id, action_sequence: pending?.sequence ?? value.actions.length + 1,
      action_kind: pending?.kind ?? value.next_action});
}
export function correctionReviewTarget(value: PublicResearchLoopV2) {
  return {jobId: value.experiment?.job_id || null, runId: value.experiment?.run_id || null};
}
