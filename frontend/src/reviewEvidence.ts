import { canReview } from "./domain";
import type { Artifact, Run } from "./domain";

/** Use the frozen run only: the editor's latest revision is a different input. */
export function reviewEvidence(run: Run, candidateId: string, artifacts: Artifact[]) {
  const candidate = run.state?.candidates?.find(item => item.id === candidateId);
  const target = run.review_targets?.find(item => item.candidate_id === candidateId);
  if (!candidate || !target || !canReview(run)) return null;
  const task = run.state?.task;
  const declared = task?.candidates?.find(item => item.id === candidateId);
  const hypothesis = task?.hypotheses?.find(item => item.id === candidate.hypothesis_id);
  const evidence = task?.evidence?.filter(item => hypothesis?.evidence_ids.includes(item.id)) || [];
  // A run may list older server attempts; candidate artifacts identify its exact
  // computation output within the selected immutable server attempt.
  // Downloads are separately verified, path-redacted exports: their digest can
  // legitimately differ from the raw engine file referenced by the candidate.
  const outputFiles = artifacts.filter(file => file.attempt_id === target.attempt_id &&
    !!candidate.artifacts && Object.hasOwn(candidate.artifacts, file.name) &&
    file.name.startsWith(`candidates/${candidateId}/`));
  const report = artifacts.find(file => file.attempt_id === target.attempt_id && file.name === "report.md");
  return { candidate, target, declared, hypothesis, evidence, outputFiles, report };
}

export function observedMetric(value: unknown, percent = false): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未计算";
  return percent ? `${(value * 100).toFixed(2)}%` : value.toFixed(6);
}
