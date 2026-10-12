import type { ProposalAbstention, ResearchV2ProposalDetail } from "./generated/api-contract";

/** Read the current validated stop response; never infer it from a raw outcome. */
export function validatedCurrentAbstention(proposal: ResearchV2ProposalDetail): ProposalAbstention | null {
  if (proposal.status !== "abstained" || !proposal.current_attempt_id || proposal.error) return null;
  const matches = proposal.attempts.filter(attempt => attempt.id === proposal.current_attempt_id);
  if (matches.length !== 1) return null;
  const attempt = matches[0];
  const output = attempt.output;
  if (attempt.status !== "abstained" || attempt.error || attempt.validation?.valid !== true ||
    output?.outcome !== "abstain" || !output.abstention) return null;
  if (output.proposal_id !== proposal.id || output.attempt_id !== attempt.id ||
    output.operation !== proposal.operation || output.context_digest !== proposal.context_digest) return null;
  return output.abstention;
}
