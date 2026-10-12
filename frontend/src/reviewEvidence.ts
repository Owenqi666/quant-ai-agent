import { canReview } from "./domain";
import type { Artifact, Candidate, Evidence, Hypothesis, Run } from "./domain";

const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const rows = <T,>(value: unknown): T[] => Array.isArray(value) ? value : [];
const nonempty = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const digest = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
export const normalizeReviewQuote = (value: string): string => value.normalize("NFKC").replace(/\s+/gu, " ").trim();
export type SourceIssue = { code: string; id?: string };
export type EvidenceReference = { id: string; evidence?: Evidence; source?: { paperId: string; documentSha256: string; pdf: Artifact; href: string }; issues: SourceIssue[] };

/** A public export is usable only when its name and ID select one file. */
function uniqueArtifact(artifacts: Artifact[], attemptId: string, name: string): Artifact | undefined {
  const matches = artifacts.filter(file => file.attempt_id === attemptId && file.name === name);
  const file = matches.length === 1 ? matches[0] : undefined;
  return file && nonempty(file.id) && artifacts.filter(item => item.id === file.id).length === 1 ? file : undefined;
}

function frozenTargetMatches(run: Run, attemptId: string): boolean {
  const target = run.regression_target;
  return !!target && target.attempt_id === attemptId && digest(target.result_digest) &&
    (run.verification?.state_digest === undefined || run.verification.state_digest === target.result_digest);
}

/** Root-level computation files belong to the closed, single-candidate schema3 producer. */
function mainOHLCLayout(run: Run, candidateId: string): boolean {
  const task = record(run.state?.task), declarations = rows<Candidate>(task.candidates);
  const candidates = rows<unknown>(run.state?.candidates);
  const declared = record(declarations[0]), candidate = record(candidates[0]);
  return task.schema_version === 3 && record(task.execution).kind === "real_daily_ohlc_development_v1" &&
    candidates.length === 1 && declarations.length === 1 && declared.id === candidateId && candidate.id === candidateId && candidateId === "alpha101-101" &&
    declared.hypothesis_id === candidate.hypothesis_id && nonempty(declared.expression) && declared.expression === candidate.expression &&
    declared.expression === record(candidate.result).expression;
}

function ordinaryOutputName(name: string, candidateId: string): boolean {
  const parts = name.split("/");
  return parts.length === 4 && parts[0] === "candidates" && parts[1] === candidateId &&
    /^attempt-[1-9][0-9]*$/.test(parts[2]) && ["factor.csv", "result.json"].includes(parts[3]);
}

/** Main literal checks are a read projection; they never replace saved state. */
function projectedSource(run: Run, attemptId: string): Record<string, unknown> | undefined {
  const projection = record(run.frozen_source);
  const target = run.regression_target;
  return projection.schema_version === 1 && projection.run_id === run.id && projection.revision_id === run.revision_id &&
    projection.attempt_id === attemptId && frozenTargetMatches(run, attemptId) && projection.state_digest === target?.result_digest &&
    nonempty(projection.paper_id) && digest(projection.document_sha256) && Array.isArray(projection.evidence) ? projection : undefined;
}

/** Only declarations from this frozen task participate in the source chain. */
function associations(run: Run, candidateId: string, hypothesisId: string) {
  const issues: SourceIssue[] = [];
  const task = run.state?.task;
  const declarations = rows<Candidate>(task?.candidates).filter(item => record(item).id === candidateId);
  const declared = declarations.length === 1 ? declarations[0] : undefined;
  if (!declared) issues.push({ code: declarations.length ? "DECLARATION_AMBIGUOUS" : "DECLARATION_MISSING", id: candidateId });
  const matchingHypothesis = declared && nonempty(declared.hypothesis_id) && declared.hypothesis_id === hypothesisId;
  if (declared && !matchingHypothesis) issues.push({ code: "HYPOTHESIS_MISMATCH", id: hypothesisId });
  const hypotheses = matchingHypothesis ? rows<Hypothesis>(task?.hypotheses).filter(item => record(item).id === hypothesisId) : [];
  const hypothesis = hypotheses.length === 1 ? hypotheses[0] : undefined;
  if (matchingHypothesis && !hypothesis) issues.push({ code: hypotheses.length ? "HYPOTHESIS_AMBIGUOUS" : "HYPOTHESIS_MISSING", id: hypothesisId });
  const evidenceReferences: EvidenceReference[] = [];
  if (hypothesis) {
    const ids = rows<unknown>(hypothesis.evidence_ids);
    if (!ids.length) issues.push({ code: "EVIDENCE_REFERENCES_MISSING", id: hypothesisId });
    for (const id of [...new Set(ids)]) {
      if (!nonempty(id)) { issues.push({ code: "EVIDENCE_ID_INVALID" }); continue; }
      const reference: EvidenceReference = { id, issues: [] };
      if (ids.filter(value => value === id).length !== 1) reference.issues.push({ code: "EVIDENCE_REFERENCE_DUPLICATE", id });
      const matches = rows<Evidence>(task?.evidence).filter(item => record(item).id === id);
      if (matches.length !== 1) reference.issues.push({ code: matches.length ? "EVIDENCE_AMBIGUOUS" : "EVIDENCE_MISSING", id });
      else if (!nonempty(matches[0].quote) || !normalizeReviewQuote(matches[0].quote) || !Number.isInteger(matches[0].page) || matches[0].page < 1) reference.issues.push({ code: "EVIDENCE_INVALID", id });
      else if (!reference.issues.length) reference.evidence = matches[0];
      evidenceReferences.push(reference);
    }
  }
  return { declared, hypothesis, evidenceReferences, issues };
}

/** Literal quote proof does not establish fidelity, mechanism truth, or AI span provenance. */
function bindPdf(run: Run, attemptId: string, candidateId: string, candidateDigest: string, reference: EvidenceReference, artifacts: Artifact[]): string | undefined {
  if (!reference.evidence) return;
  const { evidence } = reference;
  const hasProjection = run.frozen_source != null;
  const projection = hasProjection ? projectedSource(run, attemptId) : undefined;
  if (hasProjection && (!projection || !mainOHLCLayout(run, candidateId))) {
    reference.issues.push({ code: "FROZEN_SOURCE_TARGET_MISMATCH", id: evidence.id }); return;
  }
  const proofs: Record<string, unknown>[] = projection ? rows<Record<string, unknown>>(projection.evidence).map(item => ({...item,
    paper_id:projection.paper_id, document_sha256:projection.document_sha256})) : rows<Record<string, unknown>>(record(run.state?.preflight).evidence);
  const matches = proofs.filter(item => record(item).id === evidence.id);
  const proof = matches.length === 1 ? matches[0] : undefined;
  if (!proof || proof.citation_verified !== true || proof.page !== evidence.page || !nonempty(proof.quote) ||
      normalizeReviewQuote(proof.quote) !== normalizeReviewQuote(evidence.quote) || !nonempty(proof.paper_id) || !digest(proof.document_sha256)) {
    reference.issues.push({ code: matches.length > 1 ? "CITATION_PROOF_AMBIGUOUS" : "CITATION_PROOF_UNAVAILABLE", id: evidence.id }); return;
  }
  const paperId = proof.paper_id;
  const target = run.regression_target;
  if (!target || !digest(candidateDigest) || !frozenTargetMatches(run, attemptId)) {
    reference.issues.push({ code: "PDF_TARGET_UNAVAILABLE", id: evidence.id }); return paperId;
  }
  const pdfs = artifacts.filter(file => file.attempt_id === attemptId && file.name === "inputs/paper.pdf");
  const pdf = uniqueArtifact(artifacts, attemptId, "inputs/paper.pdf");
  if (!pdf || pdf.sha256 !== proof.document_sha256) {
    reference.issues.push({ code: pdfs.length > 1 ? "PDF_ARTIFACT_AMBIGUOUS" : "PDF_ARTIFACT_UNAVAILABLE", id: evidence.id }); return paperId;
  }
  const query = new URLSearchParams({ inline: "true", expected_attempt_id: attemptId,
    expected_state_digest: target.result_digest, expected_sha256: pdf.sha256 });
  reference.source = { paperId, documentSha256: proof.document_sha256, pdf,
    href: `/api/runs/${encodeURIComponent(run.id)}/artifacts/${encodeURIComponent(pdf.id)}?${query}#page=${evidence.page}` };
  return paperId;
}

/** Use the frozen run only: the editor's latest revision is a different input. */
export function reviewEvidence(run: Run, candidateId: string, artifacts: Artifact[]) {
  const candidates = rows<NonNullable<NonNullable<Run["state"]>["candidates"]>[number]>(run.state?.candidates).filter(item => record(item).id === candidateId);
  const targets = rows<NonNullable<Run["review_targets"]>[number]>(run.review_targets).filter(item => record(item).candidate_id === candidateId);
  if (candidates.length !== 1 || targets.length !== 1 || !canReview(run)) return null;
  const candidate = candidates[0], target = targets[0];
  if (!nonempty(target.attempt_id) || !nonempty(target.result_digest)) return null;
  const association = associations(run, candidateId, candidate.hypothesis_id);
  const proofPaperIds = new Set(association.evidenceReferences.map(reference => bindPdf(run, target.attempt_id, candidateId, target.result_digest, reference, artifacts)).filter(nonempty));
  if (proofPaperIds.size > 1) {
    association.issues.push({ code: "PAPER_IDENTITY_CONFLICT" });
    // One frozen task executes against one PDF. Identical bytes cannot silently
    // collapse conflicting source identities into an apparently known paper.
    association.evidenceReferences.forEach(reference => { delete reference.source; });
  }
  // Source uncertainty must not hide independently verified computed metrics.
  const evidence = association.evidenceReferences.flatMap(reference => reference.evidence ? [reference.evidence] : []);
  const sourceIssues = [...association.issues, ...association.evidenceReferences.flatMap(reference => reference.issues)];
  // Downloads are verified, path-redacted exports; their digest can legitimately
  // differ from the raw engine file referenced by the candidate.
  const mainLayout = mainOHLCLayout(run, candidateId);
  const frozenTarget = digest(target.result_digest) && frozenTargetMatches(run, target.attempt_id);
  // Legacy runs without a state target retain their already verified computed
  // exports. A present but conflicting target never grants download access.
  const outputTarget = run.regression_target == null ? !mainLayout : frozenTarget;
  const outputFiles = outputTarget ? Object.keys(candidate.artifacts || {}).flatMap(name => {
    if (!(mainLayout ? ["factor.csv", "result.json"].includes(name) : ordinaryOutputName(name, candidateId))) return [];
    const file = uniqueArtifact(artifacts, target.attempt_id, name);
    return file ? [file] : [];
  }) : [];
  const report = outputTarget ? uniqueArtifact(artifacts, target.attempt_id, "report.md") : undefined;
  const pdf = frozenTarget ? uniqueArtifact(artifacts, target.attempt_id, "inputs/paper.pdf") : undefined;
  const projection = projectedSource(run, target.attempt_id);
  const frozenPaper = pdf && digest(pdf.sha256) && (run.frozen_source == null ||
    (mainLayout && projection && pdf.sha256 === projection.document_sha256)) ? pdf : undefined;
  const taskFile = frozenTarget ? uniqueArtifact(artifacts, target.attempt_id, "inputs/task.json") : undefined;
  const candidateFile = frozenTarget && mainLayout ? uniqueArtifact(artifacts, target.attempt_id, "candidate.json") : undefined;
  // Ordinary producer declarations live inside inputs/task.json. Export only
  // the uniquely associated frozen declaration, with explicit projection copy.
  const candidateExportHref = frozenTarget && !mainLayout && association.declared ?
    "data:application/json;charset=utf-8," + encodeURIComponent(JSON.stringify(association.declared, null, 2) + "\n") : undefined;
  return { candidate, target, ...association, evidence, sourceIssues, outputFiles, report,
    frozenPaper, taskFile, candidateFile, candidateExportHref, mainLayout };
}

export function observedMetric(value: unknown, percent = false): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未计算";
  return percent ? `${(value * 100).toFixed(2)}%` : value.toFixed(6);
}
