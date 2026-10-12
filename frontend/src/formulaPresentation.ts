import type {Run} from './domain';
import type {FormulaAlignmentTarget} from './formulaAlignment';
import {formulaAlignmentPath, formulaAlignmentTarget, formulaAlignmentTargetKey} from './formulaAlignment';
import {assertApiResponse} from './generated/api-contract';
export type FormulaReadingRole = 'paper_reference' | 'declared' | 'executed';
export type FormulaReadingItem = {role: FormulaReadingRole; status: 'available' | 'unsupported' | 'missing'; expression: string | null; latex: string | null};
export type FormulaPresentationResponse = {schema_version:1;kind:'run_formula_presentation';target:FormulaAlignmentTarget;
  policy_version:'bounded-local-dsl-latex-v1';items:FormulaReadingItem[];alignment_public_view_digest:string;
  public_projection_redacted:boolean;public_view_digest:string;semantic_fidelity:'unverified';human_review_written:false;llm_api_called:false;factor_computation_called:false};
export const FORMULA_READING_ROLES: FormulaReadingRole[] = ['paper_reference','declared','executed'];
export function formulaPresentationPath(target: FormulaAlignmentTarget): string {
  return formulaAlignmentPath(target).replace('/formula-alignment?', '/formula-presentation?');
}
/** Reject every TeX command outside the existing backend display vocabulary. */
export function isBoundedDerivedLatex(value: unknown): value is string {
  if (typeof value !== 'string' || /<\/?[A-Za-z]/.test(value) || value.length === 0 || value.length > 65536 || !/^[A-Za-z0-9_ \t\r\n.+*/(),<>=!{}\\-]+$/.test(value)) return false;
  const commands = value.match(/\\(?:[A-Za-z]+|[^A-Za-z])/g) || [];
  return commands.every(command => ['\\operatorname','\\left','\\right','\\frac','\\cdot','\\le','\\ge','\\ne','\\mathtt','\\_'].includes(command)) && !value.endsWith('\\');
}
export function assertFormulaPresentation(value: unknown, target: FormulaAlignmentTarget, run: Run): asserts value is FormulaPresentationResponse {
  assertApiResponse(`/api${formulaPresentationPath(target)}`, 'GET', 200, value);
  const response = value as FormulaPresentationResponse;
  const fail = () => {throw Error('FORMULA_PRESENTATION_RESPONSE_MISMATCH');};
  const current = formulaAlignmentTarget(run,target.candidate_id);
  if (!current || formulaAlignmentTargetKey(current) !== formulaAlignmentTargetKey(target) || Object.entries(target).some(([key, expected]) => response.target[key as keyof FormulaAlignmentTarget] !== expected)) return fail();
  if (response.items.length !== 3 || response.items.some((item,index) => item.role !== FORMULA_READING_ROLES[index])) return fail();
  for (const item of response.items) {
    if ((item.status === 'available' && (!item.expression || !item.latex)) || (item.status === 'missing') !== (item.expression === null) || (item.status === 'available') !== (item.latex !== null) || (item.latex !== null && !isBoundedDerivedLatex(item.latex))) return fail();
  }
  const declared = run.state?.task?.candidates?.filter(item => item.id === target.candidate_id) || [];
  const executed = run.state?.candidates?.filter(item => item.id === target.candidate_id) || [];
  if (declared.length !== 1 || executed.length !== 1) return fail();
  if (!response.public_projection_redacted && (response.items[1].expression !== declared[0].expression || response.items[2].expression !== (executed[0].result?.expression ?? null))) return fail();
}
