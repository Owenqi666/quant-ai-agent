"""Read-only exact-target formula facts; no provider or review writes."""
from pathlib import Path
import re

from fastapi import APIRouter, Query
from pydantic import ValidationError

from ..formula_alignment import analyze_alignment
from ..storage import digest, json_text
from .evidence_lineage import EvidenceLineage, _unique, MAX_RESPONSE_BYTES
from .formula_alignment_schema import FormulaAlignmentResponse
from .service import ServiceError


class FormulaAlignmentError(ServiceError):
    def __init__(self, message, status=409, *, code='FORMULA_ALIGNMENT_INTEGRITY'):
        super().__init__(message, status)
        self.code, self.retryable = code, False


class FormulaAlignment:
    def __init__(self, store):
        self.store = store
        self.reader = EvidenceLineage(store)

    @staticmethod
    def _projection(state, hypothesis, evidence, target):
        candidate = _unique(state['task']['candidates'], target['candidate_id'], 'Task candidate')
        result = _unique(state['candidates'], target['candidate_id'], 'Execution candidate').get('result')
        preflight = state.get('preflight')
        fields = preflight.get('available_fields') if isinstance(preflight, dict) else None
        analysis = analyze_alignment(hypothesis=hypothesis, evidence=evidence, candidate=candidate,
                                     computed_result=result, available_fields=fields)
        if fields is None:
            analysis['checks'][0] = {'dimension': 'expression_legality', 'status': 'unknown',
                'code': 'FIELD_AVAILABILITY_UNRECORDED',
                'message': 'This historical run did not record its available-field set; structural comparison does not establish actual field availability.'}
        return {'schema_version': 1, 'kind': 'run_formula_alignment', 'target': target, **analysis}

    def _source_marks(self, connection, row, state, target):
        """Fence only this candidate's actual result and verified export.

        Frozen state owns the exact tool-attempt path. Missing computation is a
        supported unknown result, not permission to search for another output.
        """
        candidate = _unique(state['candidates'], target['candidate_id'], 'Execution candidate')
        if candidate.get('result') is None:
            return ()
        result, attempts = candidate['result'], candidate.get('attempts')
        if not isinstance(result, dict) or not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
            raise FormulaAlignmentError('Execution result attempt binding is malformed')
        successful = [item for item in attempts if item.get('status') in {'evaluated', 'not_evaluable'}]
        if (len(successful) != 1 or not attempts or successful[0] != attempts[-1]
                or candidate.get('status') != successful[0]['status'] or result.get('status') != successful[0]['status']
                or type(successful[0].get('number')) is not int or not 1 <= successful[0]['number'] <= 5):
            raise FormulaAlignmentError('Execution result lacks its exact successful tool attempt')
        name = f"candidates/{target['candidate_id']}/attempt-{successful[0]['number']}/result.json"
        artifacts = candidate.get('artifacts')
        if (not isinstance(artifacts, dict) or name not in artifacts
                or not isinstance(artifacts[name], str) or not re.fullmatch(r'[0-9a-f]{64}', artifacts[name])):
            raise FormulaAlignmentError('Execution result is absent from its frozen artifact inventory')
        exported = connection.execute('SELECT * FROM artifacts WHERE run_id=? AND attempt_id=? AND name=?',
                                     (row['id'], row['attempt_id'], name)).fetchall()
        if (len(exported) != 1 or not isinstance(exported[0]['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', exported[0]['sha256'])):
            raise FormulaAlignmentError('Execution result export lacks its exact frozen binding')
        # Raw engine and public export are different byte domains. The reused
        # integrity verifier checks raw against state.artifacts and export
        # against its own DB SHA independently; redaction may change its bytes.
        base = self.store.root / 'runs' / row['id'] / 'attempts' / row['attempt_id']
        original, public = base / 'output' / name, Path(exported[0]['path'])
        self.store._safe_artifact_path(original, base / 'output')
        self.store._safe_artifact_path(public, base / 'exports')
        return ((original, 16 * 1024 * 1024), (public, 16 * 1024 * 1024))

    def _publish(self, value):
        public = self.reader.public.material(value)
        public['public_projection_redacted'] = public != value
        public['public_view_digest'] = digest(public)
        if len(json_text(public).encode()) > MAX_RESPONSE_BYTES:
            raise FormulaAlignmentError('Complete formula facts exceed the response bound; nothing truncated',
                                        413, code='FORMULA_ALIGNMENT_TOO_LARGE')
        try:
            return FormulaAlignmentResponse.model_validate(public).model_dump()
        except ValidationError as exc:
            if public['public_projection_redacted'] and any(error['type'] == 'string_too_long' for error in exc.errors()):
                raise FormulaAlignmentError('Public redaction exceeds the bounded schema; nothing truncated',
                                            413, code='FORMULA_ALIGNMENT_PUBLIC_VIEW_TOO_LARGE') from exc
            raise FormulaAlignmentError('Public formula representation failed verification') from exc

    def get(self, run_id, *, candidate_id, expected_attempt_id, expected_state_digest, expected_result_digest):
        return self.reader._read(run_id, candidate_id=candidate_id, expected_attempt_id=expected_attempt_id,
            expected_state_digest=expected_state_digest, expected_result_digest=expected_result_digest,
            projection=self._projection, publish=self._publish, source_marks=self._source_marks)


def create_formula_alignment_router(store):
    router, service = APIRouter(), FormulaAlignment(store)

    @router.get('/api/runs/{run_id}/formula-alignment', response_model=FormulaAlignmentResponse)
    def get_formula_alignment(run_id: str,
            candidate_id: str = Query(..., pattern=r'^[A-Za-z0-9_-]{1,64}$'),
            expected_attempt_id: str = Query(..., min_length=1, max_length=128),
            expected_state_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$'),
            expected_result_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$')):
        return service.get(run_id, candidate_id=candidate_id, expected_attempt_id=expected_attempt_id,
                           expected_state_digest=expected_state_digest, expected_result_digest=expected_result_digest)
    return router
