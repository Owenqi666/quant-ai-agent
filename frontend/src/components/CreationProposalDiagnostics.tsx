import type { ResearchV2ProposalDetail } from "../generated/api-contract";
import { useI18n } from "../i18n";
import { validatedCurrentAbstention } from "../validatedCurrentAbstention";

const stopped = new Set(["rejected", "failed", "exhausted", "expired", "stale", "interrupted", "cancelled"]);

/** A read-only view of server validation, never a retry or an acceptance action. */
export default function CreationProposalDiagnostics({ proposal }: { proposal: ResearchV2ProposalDetail }) {
  const { t } = useI18n();
  const attempts = proposal.attempts.filter(attempt => attempt.error || attempt.validation?.valid === false ||
    attempt.validation?.diagnostics.length || attempt.validation?.candidate_checks.some(check => check.status === "blocked"));
  const abstention = validatedCurrentAbstention(proposal);
  const hasFailureDetails = stopped.has(proposal.status) || attempts.length > 0;
  if (!hasFailureDetails && !abstention) return null;
  return <>
    {abstention && <section aria-label={t("停止原因与待补资料", "Stop reason and missing requirements")}>
      <p role="status">{t("本次提案已停止。", "This proposal has stopped.")}</p>
      <p><strong>{abstention.reason_code}</strong>: {abstention.reason}</p>
      {!!abstention.missing_requirements.length && <ul aria-label={t("待补资料", "Missing requirements")}>
        {abstention.missing_requirements.map((requirement, index) => <li key={index}>{requirement}</li>)}
      </ul>}
      <p>{t("查看停止理由和刷新已有状态不调用模型；本次请求不会自动重试。", "Viewing this stop reason and refreshing existing state makes no model call; this request will not retry automatically.")}</p>
    </section>}
    {hasFailureDetails && <section aria-label={t("提案失败与校验详情", "Proposal failure and validation details")}>
    <p>{t("查看以下失败记录或点击“刷新已有状态”只读取已保存的结果，不调用模型，也不消耗 Provider 调用额度。", "Viewing these saved failures or choosing Refresh existing state only reads saved results; it makes no model call and consumes no provider dispatch allowance.")}</p>
    {stopped.has(proposal.status) && <p role="status">{t("本次请求已停止，不会自动重试。失败记录仍然保留；新的模型请求需要你明确发起，并受剩余授权和预算限制。", "This request has stopped and will not retry automatically. Its failure record is retained; any new model request requires your explicit action and remaining authorization and budget.")}</p>}
    {attempts.map(attempt => <article key={attempt.id}>
      <h4>{t(`第 ${attempt.sequence} 次尝试`, `Attempt ${attempt.sequence}`)} · {attempt.status}</h4>
      {attempt.error && <p><strong>{attempt.error.code}</strong>: {attempt.error.message}</p>}
      {attempt.validation?.valid === false && <p>{t("该响应未通过校验，未作为可采纳结果。", "This response failed validation and is not an accepted result.")}</p>}
      {!!attempt.validation?.diagnostics.length && <ul aria-label={t("具体校验诊断", "Specific validation diagnostics")}>
        {attempt.validation.diagnostics.map((diagnostic, index) => <li key={`${diagnostic.code}:${index}`}><strong>{diagnostic.code}</strong>: {diagnostic.message}</li>)}
      </ul>}
      {attempt.validation?.candidate_checks.filter(check => check.status === "blocked").map((check, index) => <div key={`${check.candidate_id}:${index}`}>
        <p>{t("被阻断的候选", "Blocked candidate")}: <code>{check.candidate_id}</code> · <strong>{check.code}</strong>: {check.message}</p>
        <p>{t("提交的表达式", "Submitted expression")}: <code>{check.submitted_expression}</code></p>
      </div>)}
      {attempt.output?.outcome === "abstain" && attempt.id !== proposal.accepted_attempt_id && <p>{t("模型提出停止，但此响应尚未被采纳；停止理由不能替代校验通过。", "The model proposed abstention, but this response has not been accepted; its reason does not establish validation success.")} {attempt.output.abstention?.reason}</p>}
    </article>)}
    {!attempts.length && <p>{t("当前公开记录没有更详细的校验诊断；可展开下方回执核对现有错误，不会重新调用。", "No more specific validation diagnostics are present in the public record. Expand the receipts below to inspect the recorded error without another call.")}</p>}
    </section>}
  </>;
}
