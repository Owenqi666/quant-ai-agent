import type { Review, Run } from "./domain";
import type { Issue } from "./feedback";

export function suggestedDisposition(category: string): string {
  return category === "evidence" ? "evidence_update" : category === "hypothesis" ? "hypothesis_change" : category === "data" ? "data_change" : "implementation_fix";
}

/** Selection aid only; the server revalidates the immutable output at closure. */
export function resolutionReviews(issue: Issue, run: Run | null): Review[] {
  if (!run || run.research_id !== issue.research_id || !run.verification?.verified) return [];
  const target = run.review_targets?.find(item => item.candidate_id === issue.candidate_id);
  if (!target) return [];
  return (run.reviews || []).filter(review => review.id !== issue.review_id && review.verdict === "accepted"
    && review.run_id === run.id && review.revision_id === run.revision_id
    && review.candidate_id === issue.candidate_id && review.attempt_id === target.attempt_id
    && review.result_digest === target.result_digest);
}
