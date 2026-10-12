import type { AcceptedMaterialDraft, AcceptedGroundedMaterialDraft, PublicExperiment, PublicMaterialEngineering, PublicResearchSession, ResearchV2ProposalDetail } from "./generated/api-contract";
import type { JsonObject } from "./domain";
import { isResearchDraft } from "./drafts";
import { readMutation, recoveryKey, writeDurably } from "./reviewRecovery";
import type { PendingMutation } from "./reviewRecovery";

export interface FlowOperationReference { inputKey: string; sourceDigest: string; sessionId: string; proposalId?: string }
export interface EngineeringMethodPolicy extends JsonObject {
  schema_version: 1; kind: "daily_formula_engineering_v1"; operator_semantics_version: string;
  allow_field_substitution: false; evaluation_period: "validation";
}
export function isEngineeringMethodPolicy(value: unknown): value is EngineeringMethodPolicy {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as EngineeringMethodPolicy;
  return row.schema_version === 1 && row.kind === "daily_formula_engineering_v1" && typeof row.operator_semantics_version === "string" && !!row.operator_semantics_version &&
    row.allow_field_substitution === false && row.evaluation_period === "validation";
}
export interface ResearchCreationFlowState {
  version: 1;
  selectedPages: number[];
  providerConfigId: string;
  researchFocus?: string;
  hostGrantId: string | null;
  extraction?: FlowOperationReference;
  materialId?: string;
  materialVersionId?: string;
  materialDigest?: string;
  engineeringMethodPolicy?: EngineeringMethodPolicy;
  generation?: FlowOperationReference;
  experimentId?: string;
}
export const emptyCreationFlow = (): ResearchCreationFlowState => ({ version: 1, selectedPages: [], providerConfigId: "", hostGrantId: null });
export function isResearchCreationFlow(value: unknown): value is ResearchCreationFlowState {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as ResearchCreationFlowState;
  if (row.version !== 1 || !Array.isArray(row.selectedPages) || row.selectedPages.length > 20 ||
    !row.selectedPages.every((page, index) => Number.isInteger(page) && page > 0 && page <= 500 && (!index || row.selectedPages[index - 1] < page)) ||
    (row.researchFocus !== undefined && (typeof row.researchFocus !== "string" || row.researchFocus.length > 2000)) ||
    typeof row.providerConfigId !== "string" || !(row.hostGrantId === null || typeof row.hostGrantId === "string")) return false;
  for (const key of ["materialId", "materialVersionId", "materialDigest", "experimentId"] as const) if (row[key] !== undefined && typeof row[key] !== "string") return false;
  for (const ref of [row.extraction, row.generation]) if (ref !== undefined && (!ref || typeof ref.inputKey !== "string" || typeof ref.sourceDigest !== "string" || typeof ref.sessionId !== "string" || (ref.proposalId !== undefined && typeof ref.proposalId !== "string"))) return false;
  if (row.engineeringMethodPolicy !== undefined && !isEngineeringMethodPolicy(row.engineeringMethodPolicy)) return false;
  return true;
}
export const creationInputKey = (value: unknown) => JSON.stringify(value);
export function proposalExecutionKind(proposal: Pick<ResearchV2ProposalDetail, "provider_mode" | "attempts">): "fixture" | "native_provider_invocation" | "not_observed" {
  if (proposal.provider_mode === "fixture") return "fixture";
  return proposal.attempts.some(attempt => attempt.receipt?.invocation_started && attempt.receipt.fixture === false) ? "native_provider_invocation" : "not_observed";
}
export const creationFlowMutationKey = (workspace: string, draft: string) => recoveryKey(workspace, "creation-ai", draft);
export function readCreationFlowMutation(storage: Pick<Storage, "getItem">, key: string): PendingMutation | null {
  const raw = storage.getItem(key);
  if (!raw) return null;
  const parsed = JSON.parse(raw);
  if (typeof parsed.path !== "string" || !/^\/research-v2\/(sessions(?:\/[^/]+\/(?:cancel|reconcile))?|proposals(?:\/[^/]+\/material)?|materials\/[^/]+\/(?:adopt|engineering)|experiments(?:\/[^/]+\/(advance|cancel))?)$/.test(parsed.path)) throw new Error("AI 恢复请求路径无效；已保留原记录。");
  const item = readMutation(storage, key, parsed.path);
  if (!item?.context || typeof item.context.action !== "string" || typeof item.context.inputKey !== "string") throw new Error("AI 恢复请求缺少来源信息；已保留原记录。");
  return item;
}
/** Persist identities to the original draft before acknowledging their request. Model output stays server-owned. */
export function persistCreationFlow(storage: Pick<Storage, "getItem" | "setItem">, key: string, flow: ResearchCreationFlowState, expected?: ResearchCreationFlowState) {
  const raw = storage.getItem(key);
  if (!raw) throw new Error("创建草稿尚未持久保存，未确认 AI 请求。请保留恢复记录。");
  const draft = JSON.parse(raw);
  if (draft.version !== 1 || draft.scope !== key || !isResearchDraft(draft.value)) throw new Error("创建草稿身份不一致，未覆盖原记录。");
  if (expected && creationInputKey(draft.value.creationFlow ?? emptyCreationFlow()) !== creationInputKey(expected)) throw new Error("创建流程已在另一窗口更新，未覆盖新内容。请刷新检查原请求。");
  if (!isResearchCreationFlow(flow)) throw new Error("AI 流程引用格式无效。");
  writeDurably(storage, key, { ...draft, savedAt: new Date().toISOString(), value: { ...draft.value, creationFlow: flow } });
}
export function selectMaterialHypotheses(material: AcceptedMaterialDraft | AcceptedGroundedMaterialDraft, selected: string[]): AcceptedMaterialDraft | AcceptedGroundedMaterialDraft {
  const hypotheses = material.hypotheses.filter(item => selected.includes(item.id));
  if (!hypotheses.length) throw new Error("请至少选择一条假说。");
  const evidenceIds = new Set(hypotheses.flatMap(item => [...item.evidence_ids, ...item.required_data.flatMap(field => field.evidence_ids)]));
  if ("support_bindings" in material) {
    material.paper_statistics.forEach(item => item.evidence_ids.forEach(id => evidenceIds.add(id)));
    return { ...material, hypotheses, support_bindings: material.support_bindings.filter(item => selected.includes(item.hypothesis_id)),
      evidence: material.evidence.filter(item => evidenceIds.has(item.id)) };
  }
  return { ...material, hypotheses, evidence: material.evidence.filter(item => evidenceIds.has(item.id)) };
}
export function applyEngineeringConfiguration(before: ResearchCreationFlowState, result: PublicMaterialEngineering): ResearchCreationFlowState {
  if (!isEngineeringMethodPolicy(result.method_policy) || result.version.actor !== "automation" || result.human_approval !== false ||
    result.verification_scope !== "formula_engineering_only" || result.human_review_status !== "pending_human") throw new Error("工程配置回执范围不一致，未覆盖原流程。");
  return { ...before, materialId: result.version.material_id, materialVersionId: result.version.id, materialDigest: result.version.digest,
    engineeringMethodPolicy: result.method_policy, generation: undefined, experimentId: undefined };
}
export function nextExperimentAction(experiment: PublicExperiment): "validate" | "commit" | "submit" | "observe" | "complete" | null {
  const next = { created: "validate", validated: "commit", committed: "submit", submitted: "observe", observed: "complete" } as const;
  return experiment.job && experiment.job.state in next ? next[experiment.job.state as keyof typeof next] : null;
}

/** Reconcile only the generation receipt owned by this exact proposal. */
export function generationReconciliationToken(session: PublicResearchSession, proposalId: string): string | null {
  const matches = session.reservations.filter(item => item.action === "generate" && item.children?.proposal_id === proposalId);
  if (matches.length !== 1) throw new Error("候选与会话保留记录不唯一匹配，未交接实验。");
  return matches[0].status === "finished" ? null : matches[0].token;
}
export function creationMutationBody(receipt: PendingMutation): Record<string, unknown> {
  if (receipt.context?.action === "reconcile_session") {
    if (!/^\/research-v2\/sessions\/[^/]+\/reconcile$/.test(receipt.path) || typeof receipt.body.token !== "string" || !receipt.body.token) throw new Error("会话核对请求格式不匹配，未发送。");
    // This closed endpoint is idempotent by the frozen reservation token. The
    // local receipt nonce remains local; it is not an accepted API field.
    return { token: receipt.body.token };
  }
  return receipt.body;
}
