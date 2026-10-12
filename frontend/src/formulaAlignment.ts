import { lineageTarget, lineageTargetKey } from "./evidenceLineage";
import type { LineageTarget } from "./evidenceLineage";
import type { Run } from "./domain";
import { assertApiResponse } from "./generated/api-contract";
import type { FormulaAlignmentResponse } from "./generated/api-contract";
export type { FormulaAlignmentResponse } from "./generated/api-contract";
export type FormulaAlignmentTarget = LineageTarget;
export const formulaAlignmentTarget = lineageTarget;
export const formulaAlignmentTargetKey = lineageTargetKey;
export const FORMULA_UNASSESSED = ["economic_mechanism", "natural_language_direction", "field_units", "calendar_and_missing_policy", "algebraic_equivalence", "overall_hypothesis_fidelity"] as const;
const CHECK_DIMENSIONS = ["expression_legality", "formula_reference", "field_declarations", "execution_identity", "attribution_declarations"];
export function formulaAlignmentPath(target: FormulaAlignmentTarget): string {
  const query = new URLSearchParams({candidate_id: target.candidate_id, expected_attempt_id: target.attempt_id, expected_state_digest: target.state_digest, expected_result_digest: target.result_digest});
  return `/runs/${encodeURIComponent(target.run_id)}/formula-alignment?${query}`;
}
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
export function assertFormulaTarget(value: unknown, target: FormulaAlignmentTarget): void {
  if (!object(value) || value.schema_version !== 1 || value.kind !== "run_formula_alignment" || !object(value.target) || Object.entries(target).some(([key, expected]) => (value.target as Record<string, unknown>)[key] !== expected)) throw new Error("FORMULA_ALIGNMENT_TARGET_MISMATCH");
}
const equal = (left: unknown, right: unknown): boolean => JSON.stringify(left) === JSON.stringify(right);
/** The server proves frozen source digests and parses Python ASTs. The client only
 * validates the generated public contract, exact target and public associations. */
export function assertFormulaAlignment(value: unknown, target: FormulaAlignmentTarget, run: Run): asserts value is FormulaAlignmentResponse {
  assertFormulaTarget(value, target);
  assertApiResponse(`/api${formulaAlignmentPath(target)}`, "GET", 200, value);
  const response = value as FormulaAlignmentResponse;
  const fail = () => { throw new Error("FORMULA_ALIGNMENT_RESPONSE_MISMATCH"); };
  const currentTarget = formulaAlignmentTarget(run, target.candidate_id);
  if (!currentTarget || formulaAlignmentTargetKey(currentTarget) !== formulaAlignmentTargetKey(target)) return fail();
  const declared = run.state?.task?.candidates?.filter(item => item.id === target.candidate_id) || [];
  const executed = run.state?.candidates?.filter(item => item.id === target.candidate_id) || [];
  if (declared.length !== 1 || executed.length !== 1 || declared[0].hypothesis_id !== executed[0].hypothesis_id || response.candidate.hypothesis_id !== declared[0].hypothesis_id || response.candidate.origin !== declared[0].origin) return fail();
  const hypotheses = run.state?.task?.hypotheses?.filter(item => item.id === declared[0].hypothesis_id) || [];
  if (hypotheses.length !== 1) return fail();
  if (!response.public_projection_redacted && (!equal(response.candidate.submitted_expression, declared[0].expression) || !equal(response.candidate.executed_expression ?? null, executed[0].result?.expression ?? null) || !equal(response.candidate.declared_changes, declared[0].changes))) return fail();
  const semantics = response.unassessed_semantics;
  if (semantics.length !== FORMULA_UNASSESSED.length || new Set(semantics).size !== semantics.length || FORMULA_UNASSESSED.some(item => !semantics.includes(item))) return fail();
  if (response.checks.length !== CHECK_DIMENSIONS.length || new Set(response.checks.map(item => item.dimension)).size !== response.checks.length || CHECK_DIMENSIONS.some(item => !response.checks.some(check => check.dimension === item))) return fail();
  if (new Set(response.reference.citations.map(item => item.evidence_id)).size !== response.reference.citations.length) return fail();
  for (const citation of response.reference.citations) {
    const evidence = run.state?.task?.evidence?.filter(item => item.id === citation.evidence_id) || [];
    if (!hypotheses[0].evidence_ids.includes(citation.evidence_id) || evidence.length !== 1 || evidence[0].page !== citation.page) return fail();
  }
  const referenceKnown = response.reference.status === "identified";
  if (referenceKnown && (!response.reference.expression || response.reference.mode === "none" || response.reference.citations.length === 0 || !response.features.reference)) return fail();
  if (!referenceKnown && (response.reference_to_declared.status !== "unknown" || response.features.reference !== null)) return fail();
  const both = !!response.features.reference && !!response.features.declared;
  if ((response.reference_to_declared.status !== "unknown") !== both) return fail();
  if (both && response.features.structure_changed !== (response.reference_to_declared.status === "different")) return fail();
  if (!both && (response.features.structure_changed !== null || response.features.window_changed !== null || response.features.constants_changed !== null || response.features.outer_transform !== null)) return fail();
  if (response.features.outer_transform && response.reference_to_declared.status !== "different") return fail();
  if (response.execution.status === "unavailable" && (response.candidate.executed_expression != null || response.declared_to_executed.status !== "unknown")) return fail();
}
