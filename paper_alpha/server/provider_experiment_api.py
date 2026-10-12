"""Explicit handoff into session-authorized experiments; no new worker queue."""
from fastapi import APIRouter
from .provider_research_api import _ResearchViews
from .proposal_requests import ProposalRequests
from .research_session_experiments import SessionExperiments
from .provider_correction_public_schema import ExperimentCreateRequest, PublicExperimentResponse
from .provider_correction_public_projection import project_public_experiment_v2
from .provider_experiment_api_schema import PublicExperiment, ExperimentAdvance, ExperimentCancel
from .provider_experiment_results import build_experiment_result, ExperimentResult
from .research_jobs_schema import ResearchJobSummary
from .response_schemas import ErrorResponse


def create_experiment_router(store, registry):
    service = SessionExperiments(store)
    views = _ResearchViews(store, registry, ProposalRequests(store, configs=registry.configs))
    router = APIRouter(prefix='/api/research-v2/experiments', tags=['session-experiments'],
        responses={code: {'model': ErrorResponse} for code in (400, 403, 404, 409, 413, 422, 429, 500, 503)})

    def public(value):
        if value['request'].get('schema_version', 1) == 2:
            return project_public_experiment_v2(value, public_session=views.session(value['session']),
                public_reservation=views.reservation(value['reservation']), redact=views.material)
        job = value['job']
        plan = value['plan']
        original = {'schema_version': 1, 'id': value['id'], 'created_at': value['created_at'],
            'session_id': value['request']['session_id'], 'proposal_id': value['request']['proposal_id'],
            'accepted_response_digest': value['request']['accepted_response_digest'],
            'request_digest': value['request_digest'], 'task_digest': plan['task_digest'],
            'source_kind': plan['source_request']['kind'], 'budget': value['request']['budget'],
            'bootstrap': value['bootstrap'],
            'job': {key: job[key] for key in ResearchJobSummary.model_fields} if job else None,
            'session': views.session(value['session']), 'reservation': views.reservation(value['reservation']),
            'candidate_checks': plan['candidate_checks'], 'semantic_fidelity': 'unverified',
            'budget_scope': 'session_top_level_actions_and_experiments_with_separate_job_steps'}
        projected = views.material(original)
        return {**projected, 'public_projection_redacted': projected != original
                or projected['session']['public_projection_redacted']
                or views.material(value['reservation']) != value['reservation']}

    @router.post('', response_model=PublicExperimentResponse, status_code=201)
    def create(body: ExperimentCreateRequest):
        return views.call(public, views.call(service.create, body))

    @router.get('/{experiment_id}', response_model=PublicExperimentResponse)
    def get(experiment_id: str):
        return views.call(public, views.call(service.get, experiment_id))

    @router.post('/{experiment_id}/advance', response_model=PublicExperimentResponse)
    def advance(experiment_id: str, body: ExperimentAdvance):
        return views.call(public, views.call(service.advance, experiment_id, body.action, body.idempotency_key))

    @router.post('/{experiment_id}/cancel', response_model=PublicExperimentResponse)
    def cancel(experiment_id: str, body: ExperimentCancel):
        return views.call(public, views.call(service.cancel, experiment_id, body.idempotency_key))

    @router.post('/{experiment_id}/reconcile', response_model=PublicExperimentResponse)
    def reconcile(experiment_id: str):
        return views.call(public, views.call(service.reconcile, experiment_id))

    @router.get('/{experiment_id}/result', response_model=ExperimentResult)
    def result(experiment_id: str):
        item = views.call(service.get, experiment_id)
        if item['job'] is None:
            from .proposal_requests import ProposalServiceError
            raise ProposalServiceError('Experiment has no completed job result', code='RESULT_NOT_READY')
        # Result projection already verifies its exact completed job and run.
        return views.call(build_experiment_result, store, item['job']['id'])

    return router
