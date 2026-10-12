import {translate} from "./i18n";
import type { ProposalContextResponse, ProposalDetail, ProposalResearchReview, ProposalSummary, ProvidersResponse } from "./generated/api-contract";

const active = new Set(["queued", "running", "validating", "retry_wait", "cancelling", "handing_off"]);
export const proposalIsActive = (status: string) => active.has(status);
export const proposalCanCancel = (status: string) => ["queued", "running", "validating", "retry_wait"].includes(status);
export const proposalCanHandoff = (value: ProposalDetail) => value.status === "ready" && !!value.accepted_response_digest && value.accepted_response?.outcome === "draft";
const labels: Record<string, string> = {queued:"等待生成",running:"正在生成",validating:"正在校验",retry_wait:"等待重试",ready:"草案可交接实验",handing_off:"正在交接",handed_off:"已交接实验",abstained:"模型弃权",rejected:"机器校验拒绝",failed:"生成失败",cancelled:"已取消",cancelling:"停止中",expired:"生成超时",stale:"来源已变化",interrupted:"执行被中断",exhausted:"预算耗尽"};
const englishLabels: Record<string,string> = {queued:"Waiting for generation",running:"Generating",validating:"Validating",retry_wait:"Waiting to retry",ready:"Draft ready for experiment handoff",handing_off:"Handing off",handed_off:"Handed to experiment",abstained:"Model abstained",rejected:"Machine validation rejected",failed:"Generation failed",cancelled:"Cancelled",cancelling:"Stopping",expired:"Generation timed out",stale:"Source changed",interrupted:"Execution interrupted",exhausted:"Budget exhausted"};
export const proposalStatusLabel = (status: string) => translate(labels[status] || status, englishLabels[status] || status);

export function buildProposalCreate(researchId: string, revisionId: string, context: ProposalContextResponse, providers: ProvidersResponse, configId: string, instruction: string) {
  if (context.context.research_id !== researchId || context.context.base_revision_id !== revisionId)
    throw new Error(translate("证据上下文与当前研究修订不匹配，请刷新后提交。","The evidence context does not match this research revision. Refresh before submitting."));
  const config = providers.providers.find(item => item.config_id === configId);
  if (!config?.dispatchable || providers.dispatch_paused ||
      (config.mode === "codex_cli" && !providers.real_dispatch_enabled) ||
      (config.mode === "fixture" && !providers.allow_fixture))
    throw new Error(translate("此 Provider 当前不能接收任务，请检查宿主配置及暂停状态。","This provider cannot accept a task. Check host configuration and dispatch pause."));
  return {research_id:researchId,base_revision_id:revisionId,expected_source_digest:context.context.source_digest,
    provider_config_id:configId,purpose:"generate_from_evidence",candidate_limit:1,instruction,
    budget:{max_provider_invocations:1,max_seconds:240}};
}

export function reviewMatchesProposal(review: Pick<ProposalResearchReview,"proposal">, proposal: ProposalSummary) {
  return review.proposal.id === proposal.id && review.proposal.research_id === proposal.research_id &&
    review.proposal.base_revision_id === proposal.base_revision_id && review.proposal.status === proposal.status &&
    review.proposal.accepted_response_digest === proposal.accepted_response_digest && review.proposal.research_job_id === proposal.research_job_id;
}

export function buildProposalHandoff(proposal: ProposalDetail) {
  if (!proposalCanHandoff(proposal)) throw new Error(translate("只有本次已校验的实际草案才能交接实验。","Only this verified draft can be handed to an experiment."));
  return {accepted_response_digest:proposal.accepted_response_digest!,
    budget:{max_steps:10,max_failures:3,max_seconds:300},
    note:"用户显式交接草案进入工程实验；研究质量仍待人工评审。",
    allow_partial_execution:false,method_status:"resolved",unresolved_rules:[]};
}

export const observedMetric = (value: number | null | undefined) => value == null ? translate("缺失","Missing") : String(value);
