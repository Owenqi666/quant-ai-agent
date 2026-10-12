"""Read-only LaTeX over the existing exact-target frozen formula verifier."""
from fastapi import APIRouter, Query
from ..formula_presentation import expression_to_latex
from ..storage import digest
from .formula_alignment import FormulaAlignment
from .formula_presentation_schema import FormulaPresentationResponse

class FormulaPresentation:
    def __init__(self, store):
        self.alignment = FormulaAlignment(store)

    def get(self, run_id, *, candidate_id, expected_attempt_id, expected_state_digest, expected_result_digest):
        # The original reader validates raw result/export artifacts, frozen
        # selection/revision and all target identities in its transaction.
        facts = self.alignment.get(run_id, candidate_id=candidate_id,
            expected_attempt_id=expected_attempt_id, expected_state_digest=expected_state_digest,
            expected_result_digest=expected_result_digest)
        reference = facts['reference']
        # An unsupported/unidentified reference is not scientific authority;
        # never borrow a candidate declaration for the absent reference/result.
        source = [('paper_reference', reference['expression'] if reference['status'] == 'identified' else None),
            ('declared', facts['candidate']['submitted_expression']),
            ('executed', facts['candidate']['executed_expression'])]
        items = []
        for role, expression in source:
            latex = expression_to_latex(expression) if expression is not None else None
            items.append({'role': role, 'status': 'missing' if expression is None else 'available' if latex is not None else 'unsupported',
                'expression': expression, 'latex': latex})
        value = {'schema_version': 1, 'kind': 'run_formula_presentation', 'target': facts['target'],
            'policy_version': 'bounded-local-dsl-latex-v1', 'items': items,
            'alignment_public_view_digest': facts['public_view_digest'],
            'public_projection_redacted': facts['public_projection_redacted'],
            'semantic_fidelity': 'unverified', 'human_review_written': False,
            'llm_api_called': False, 'factor_computation_called': False}
        value['public_view_digest'] = digest(value)
        return FormulaPresentationResponse.model_validate(value).model_dump()

def create_formula_presentation_router(store):
    router, service = APIRouter(), FormulaPresentation(store)

    @router.get('/api/runs/{run_id}/formula-presentation', response_model=FormulaPresentationResponse)
    def get_formula_presentation(run_id: str,
            candidate_id: str = Query(..., pattern=r'^[A-Za-z0-9_-]{1,64}$'),
            expected_attempt_id: str = Query(..., min_length=1, max_length=128),
            expected_state_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$'),
            expected_result_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$')):
        return service.get(run_id, candidate_id=candidate_id, expected_attempt_id=expected_attempt_id,
            expected_state_digest=expected_state_digest, expected_result_digest=expected_result_digest)
    return router
