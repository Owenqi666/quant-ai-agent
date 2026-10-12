"""Versioned main-lane metadata API; no raw ingestion or bootstrap endpoint."""
from contextlib import closing
from fastapi import APIRouter, Query

from .db import connect
from .service import ServiceError
from .response_schemas import ErrorResponse
from .real_daily_main_api_schema import (MainDailySourceHandle, MainDailySourceHandlePage, MainDailyAssessmentCreate, MainDailyApprovalCreate,
    MainDailyRevokeCreate, MainDailyReserveCreate, MainDailyNormalizeCreate, MainDailyAssessmentSummary, MainDailyApprovalSummary, MainDailyReservationSummary,
    MainDailyRevocationSummary, MainDailyNormalizationSummary, MainDailyRegistrationSummary, MainDailyMainCapabilities)
from .real_daily_workspace_authority import verify_workspace_metadata
from ..real_daily_main_task import MAX_MAIN_DEVELOPMENT_CELLS, MAX_MAIN_VALIDATION_CELLS


def create_real_daily_main_router(store):
    sources = store.real_daily_sources()
    prefixes = store.real_daily_prefixes()
    authority = store.real_daily_authority()
    router = APIRouter(prefix='/api/real-daily/v1', tags=['real-daily-main'],
        responses={code: {'model': ErrorResponse} for code in (400, 403, 404, 409, 422, 500)})

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, OSError) as exc:
            code = getattr(exc, 'code', 'real_daily_operation_failed')
            if type(code) is not str or not 0 < len(code) <= 128 or not all(c.isalnum() or c == '_' for c in code):
                code = 'real_daily_operation_failed'
            status = 404 if code.endswith('_missing') else 403 if code.startswith('approval_') and code not in (
                'approval_pin_mismatch', 'approval_scope_required', 'approval_binding_drift') else 409
            if code.startswith(('invalid_', 'unsafe_', 'absolute_')) or code in ('approval_scope_required', 'bounded_metadata_required'):
                status = 400
            error = ServiceError('Real-daily operation blocked: ' + code, status)
            error.code = code
            error.retryable = False
            raise error from None

    @router.get('/capabilities', response_model=MainDailyMainCapabilities)
    def capabilities():
        with closing(connect(store.db_path)) as connection:
            metadata = call(verify_workspace_metadata, connection)
        return {'schema_version': 1, 'execution_kind': 'real_daily_ohlc_development_v1',
            'data_kind': 'real_daily_ohlc_development', 'fields': ['open', 'high', 'low', 'close'],
            'task_schema_version': 3, 'supported_candidate': 'alpha101-101',
            'max_development_cells': MAX_MAIN_DEVELOPMENT_CELLS,
            'max_validation_eligible_cells': MAX_MAIN_VALIDATION_CELLS,
            'authority_initialized': metadata['initialized'], 'offline_bootstrap_required': not metadata['initialized'],
            'source_capture_mode': 'offline_controlled_metadata_handles', 'source_authenticated': False,
            'real_source_ready': False, 'point_in_time': 'not_certified', 'final_numeric_access_allowed': False}

    @router.get('/sources', response_model=MainDailySourceHandlePage, response_model_exclude_unset=True)
    def list_sources():
        items = call(sources.list)
        return {'items': items, 'total': len(items)}

    @router.get('/sources/{handle_id}', response_model=MainDailySourceHandle, response_model_exclude_unset=True)
    def source(handle_id: str):
        return call(sources.get, handle_id)

    @router.post('/assessments', response_model=MainDailyAssessmentSummary, response_model_exclude_unset=True, status_code=201)
    def create_assessment(body: MainDailyAssessmentCreate):
        return call(authority.submit_source_handle, body.handle_id, expected_digest=body.expected_handle_digest,
                    idempotency_key=body.idempotency_key)

    @router.get('/assessments/{assessment_id}', response_model=MainDailyAssessmentSummary, response_model_exclude_unset=True)
    def get_assessment(assessment_id: str):
        return call(authority.get_assessment, assessment_id)

    @router.post('/assessments/{assessment_id}/approve', response_model=MainDailyApprovalSummary, status_code=201)
    def approve(assessment_id: str, body: MainDailyApprovalCreate):
        return call(authority.approve, assessment_id, **body.model_dump(exclude={'schema_version'}))

    @router.get('/approvals/{approval_id}', response_model=MainDailyApprovalSummary)
    def get_approval(approval_id: str, expected_digest: str = Query(pattern=r'^[0-9a-f]{64}$', min_length=64, max_length=64)):
        return call(authority.current_permission, approval_id, expected_digest=expected_digest)

    @router.post('/approvals/{approval_id}/revoke', response_model=MainDailyRevocationSummary)
    def revoke(approval_id: str, body: MainDailyRevokeCreate):
        return call(authority.revoke, approval_id, **body.model_dump(exclude={'schema_version'}))

    @router.post('/approvals/{approval_id}/reserve', response_model=MainDailyReservationSummary, status_code=201)
    def reserve(approval_id: str, body: MainDailyReserveCreate):
        return call(authority.reserve, approval_id, **body.model_dump(exclude={'schema_version'}))

    @router.post('/normalizations', response_model=MainDailyNormalizationSummary, status_code=201)
    def normalize(body: MainDailyNormalizeCreate):
        return call(prefixes.normalize, **body.model_dump(exclude={'schema_version'}))

    @router.get('/normalizations/by-key/{idempotency_key}', response_model=MainDailyNormalizationSummary)
    def get_normalization_key(idempotency_key: str):
        return call(prefixes.get_normalization_by_key, idempotency_key)

    @router.get('/normalizations/{intent_id}', response_model=MainDailyNormalizationSummary)
    def get_normalization(intent_id: str):
        return call(prefixes.get_normalization, intent_id)

    @router.get('/registrations/{registration_id}', response_model=MainDailyRegistrationSummary, response_model_exclude_unset=True)
    def get_registration(registration_id: str):
        return call(prefixes.get_registration, registration_id)

    return router
