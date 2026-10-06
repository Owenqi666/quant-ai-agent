"""HTTP surface for a local-only research workbench; no LLM integration."""
from __future__ import annotations
from .db import SCHEMA_VERSION

from contextlib import asynccontextmanager
import json
import hashlib
import logging
import math
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .schemas import CaseCreate, CheckCreate, ResearchCreate, ReviewCreate, RevisionCreate, RunCreate, ImportValidate, ImportRegister, IssueCreate, IssueUpdate
from . import response_schemas as responses
from .dataset_imports import DatasetImports, DatasetImportError
from .feedback import Feedback
from .maintenance import workspace_lease
from .service import REPO, ServiceError, Store
from .research_insights import ResearchInsights
from .candidate_series import CandidateSeriesService
from .candidate_series_schema import CandidateSeriesResponse
from .research_insights_schema import RunComparison, ReportCreate, ResearchReport, ReportList
from .workflow_observations import WorkflowObservations
from .observation_context import ObservationContextService
from .observation_context_schema import ObservationContext
from ..research_protocol import ProtocolError, presets as protocol_presets, preview as protocol_preview
from .research_protocols import ResearchProtocols
from .research_protocol_schema import (ProtocolPresets, ProtocolPreview, ProtocolPreviewCreate,
                                       ProtocolCreate, ProtocolRecord, ProtocolPage)
from . import monthly_schema as monthly_responses
from .monthly_experiments import MonthlyExperiments
from .industry_mom import IndustryMomSources, IndustryMomExperiments
from .industry_mom_schema import (IndustryMomSourceDetail, IndustryMomSourcePage,
    IndustryMomCreate, IndustryMomCancel, IndustryMomRetry, IndustryMomAck,
    IndustryMomExperimentDetail, IndustryMomExperimentPage, IndustryMomStatus)
from .author_panels import AuthorPanels
from .author_studies import AuthorStudies
from .research_cases import ResearchCases
from .research_cases_schema import CaseCreate as ResearchCaseCreate, CaseDetail, CasePage, CasePreview, SourceKind
from .research_tools import ResearchTools, ToolServiceError
from .research_tools_schema import ToolSessionCreate, ToolSession, ToolSessionPage, ToolCallCreate, ToolCall, ToolCapabilities
from .research_jobs import ResearchJobs
from .research_jobs_schema import ResearchJobCreate, ResearchJobAdvance, ResearchJob, ResearchJobPage, JobStep
from .research_claims import ResearchClaims
from .research_claims_schema import ClaimPreviewRequest, ClaimCreate, ClaimPreview, ClaimDetail, ClaimPage
from .research_guard import ResearchGuard
from .research_guard_schema import GuardStatus, GuardPage
from .domain_research_jobs import DomainResearchJobs
from .domain_research_jobs_schema import DomainJobCreate, DomainJobAdvance, DomainResearchJob, DomainJobPage, DomainJobStep
from .semantic_annotations import SemanticAnnotations
from .semantic_annotations_schema import (SemanticMaterialCollection, SemanticMaterialDetail,
    SemanticAnnotationPreviewRequest, SemanticAnnotationCreate, SemanticAnnotationPreview,
    SemanticAnnotationDetail, SemanticAnnotationPage, SemanticAnnotationSummary)
from .claim_reviews import ClaimReviews
from .claim_reviews_schema import (ClaimReviewTarget, ClaimReviewPreviewRequest, ClaimReviewCreate,
    ClaimReviewPreview, ClaimReviewDetail, ClaimReviewPage, ClaimReviewStatus)
from .review_materials import ReviewMaterials
from .review_materials_schema import ReviewMaterialsDetail
from .semantic_evaluation_sets import SemanticEvaluationSets
from .semantic_evaluation_sets_schema import (SemanticEvaluationSetPreviewRequest, SemanticEvaluationSetCreate,
    SemanticEvaluationSetPreview, SemanticEvaluationSetDetail, SemanticEvaluationSetPage,
    SemanticEvaluationComparisonCreate, SemanticEvaluationComparisonDetail, SemanticEvaluationComparisonPage,
    SemanticEvaluationBundle)
from .research_bindings import ResearchBindings
from .research_bindings_schema import (BindingPrepareRequest, BindingPreviewRequest, BindingCreate,
    BindingPreview, ResearchBinding, BindingPage, BindingExport)
from .author_study_api_schema import StudyCreate, StudyDetail, StudyPage, StudyReviewCreate, StudyReview, StudyReviews
from .author_panel_api_schema import AuthorPanelCreate, AuthorPanelDetail, AuthorPanelPage
from ..monthly_evaluation import MonthlyError
from .workflow_observations_schema import (ObservationValidate, ObservationCreate, ObservationValidation,
                                           ObservationDetail, ObservationList, ObservationSummary, ObservationTemplate)


def strict_loads(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def reject(value):
        raise ValueError('Nonfinite JSON number')
    def finite(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('Nonfinite JSON number')
        return result
    return json.loads(data, object_pairs_hook=unique, parse_constant=reject, parse_float=finite)


class InputBoundary:
    """Limit chunked bodies as well as Content-Length; defend local mutation routes."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {key.decode('latin1').lower(): value.decode('latin1') for key, value in scope['headers']}
        method = scope['method']
        if method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            origin = headers.get('origin')
            expected = scope.get('scheme', 'http') + '://' + headers.get('host', '')
            if (origin is not None and origin.rstrip('/') != expected) or headers.get('sec-fetch-site') == 'cross-site':
                return await JSONResponse({'detail': 'Cross-origin mutations are not allowed'}, 403)(scope, receive, send)
            if scope['path'].startswith('/api/'):
                content_type = headers.get('content-type', '').split(';', 1)[0].strip().lower()
                multipart = scope['path'] in {'/api/papers', '/api/dataset-imports'} and content_type == 'multipart/form-data'
                limit = (18 if scope['path'] == '/api/dataset-imports' else 16) * 1024 * 1024 + 65536 if multipart else 1024 * 1024
                chunks, length = [], 0
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    data = message.get('body', b'')
                    length += len(data)
                    if length > limit:
                        return await JSONResponse({'detail': 'Request body exceeds limit'}, 413)(scope, receive, send)
                    chunks.append(data)
                    if not message.get('more_body'):
                        break
                body = b''.join(chunks)
                if body and not multipart:
                    if content_type != 'application/json':
                        return await JSONResponse({'detail': 'Use application/json'}, 415)(scope, receive, send)
                    try:
                        strict_loads(body)
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        return await JSONResponse({'detail': 'Invalid JSON: duplicate keys and nonfinite numbers are forbidden'}, 422)(scope, receive, send)
                sent = False
                async def replay():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {'type': 'http.request', 'body': body, 'more_body': False}
                    return await receive()
                return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


def create_app(root: Path | None = None):
    store = Store(root or Path(os.environ.get('PAPER_ALPHA_HOME', REPO / 'var/workbench')))
    imports = DatasetImports(store.root, store.db_path)
    feedback = Feedback(store)
    insights = ResearchInsights(store)
    candidate_series = CandidateSeriesService(store)
    observations = WorkflowObservations(store)
    observation_context = ObservationContextService(store)
    protocols = ResearchProtocols(store)
    monthly = MonthlyExperiments(store)
    industry_sources = IndustryMomSources(store)
    industry_mom = IndustryMomExperiments(store)
    author_panels = AuthorPanels(store)
    author_studies = AuthorStudies(store)
    research_cases = ResearchCases(store)
    research_tools = ResearchTools(store)
    research_jobs = ResearchJobs(store)
    research_claims = ResearchClaims(store)
    research_guard = ResearchGuard(store)
    domain_jobs = DomainResearchJobs(store)
    semantic_annotations = SemanticAnnotations(store)
    claim_reviews = ClaimReviews(store)
    review_materials = ReviewMaterials(store)
    semantic_sets = SemanticEvaluationSets(store)
    research_bindings = ResearchBindings(store)

    @asynccontextmanager
    async def lifespan(app):
        with workspace_lease(store.root):
            yield

    app = FastAPI(title='Paper-to-Alpha Workbench', version='0.22.0', lifespan=lifespan,
                  responses={status: {'model': responses.ErrorResponse} for status in (400, 403, 404, 405, 409, 412, 413, 415, 422, 429, 500)},
                  description='Local single-user research workflow. AI interfaces are intentionally disabled.')
    app.state.store = store
    app.add_middleware(InputBoundary)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', '::1', 'testserver'])

    @app.exception_handler(ServiceError)
    @app.exception_handler(DatasetImportError)
    async def service_error(request, exc):
        content = {'detail': str(exc)}
        if hasattr(exc, 'code'):
            content.update(code=exc.code, retryable=getattr(exc, 'retryable', False))
        return JSONResponse(content, exc.status)

    @app.exception_handler(ProtocolError)
    @app.exception_handler(MonthlyError)
    async def protocol_error(request, exc):
        return JSONResponse({'detail': str(exc)}, 422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo submitted data or internal validation objects into error responses.
        details = ['.'.join(str(p) for p in item['loc']) + ': ' + item['msg'] for item in exc.errors()]
        return JSONResponse({'detail': '; '.join(details)}, 422)

    @app.exception_handler(ResponseValidationError)
    async def response_validation_error(request, exc):
        # Retain useful locations without echoing internal values or paths.
        logging.getLogger(__name__).error('API response contract violated: %s %s locations=%s',
            request.method, request.url.path, [item['loc'] for item in exc.errors()])
        return JSONResponse({'detail': 'Internal response does not match API contract'}, 500)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse({'detail': str(exc.detail)}, exc.status_code)

    @app.get('/api/health', response_model=responses.HealthResponse, response_model_exclude_unset=True)
    def health():
        return {'status': 'ok', 'version': '0.22.0', 'workspace_id': hashlib.sha256(str(store.root.resolve()).encode()).hexdigest(), 'database_schema': SCHEMA_VERSION, 'ai_enabled': False, 'worker': store.worker_health()}

    @app.get('/api/research-cases/preview', response_model=CasePreview, response_model_exclude_unset=True)
    def research_case_preview(source_kind: SourceKind, source_id: str = Query(min_length=1, max_length=200)):
        return research_cases.preview(source_kind, source_id)

    @app.post('/api/research-cases', response_model=CaseDetail, status_code=201, response_model_exclude_unset=True)
    def research_case_create(body: ResearchCaseCreate):
        return research_cases.create(**body.model_dump())

    @app.get('/api/research-cases', response_model=CasePage)
    def research_case_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return research_cases.list(limit=limit, offset=offset)

    @app.get('/api/research-cases/{case_id}', response_model=CaseDetail, response_model_exclude_unset=True)
    def research_case_get(case_id: str):
        return research_cases.get(case_id)

    @app.get('/api/research-cases/{case_id}/markdown', response_class=Response,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def research_case_markdown(case_id: str):
        return Response(research_cases.markdown(case_id), media_type='text/markdown',
                        headers={'Content-Disposition': 'attachment; filename="research-case.md"'})

    @app.get('/api/research-tools/capabilities', response_model=ToolCapabilities)
    def research_tool_capabilities():
        return research_tools.capabilities()

    @app.post('/api/research-tool-sessions', response_model=ToolSession, status_code=201)
    def research_tool_session_create(body: ToolSessionCreate):
        return research_tools.create(**body.model_dump())

    @app.get('/api/research-tool-sessions', response_model=ToolSessionPage)
    def research_tool_session_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                                   case_id: str | None = Query(None, max_length=200)):
        return research_tools.list(limit=limit, offset=offset, case_id=case_id)

    @app.get('/api/research-tool-sessions/{session_id}', response_model=ToolSession)
    def research_tool_session_get(session_id: str):
        return research_tools.get(session_id)

    @app.post('/api/research-tool-sessions/{session_id}/calls', response_model=ToolCall)
    def research_tool_call(session_id: str, body: ToolCallCreate):
        return research_tools.call(session_id, **body.model_dump())

    @app.post('/api/research-jobs', response_model=ResearchJob, status_code=201)
    def research_job_create(body: ResearchJobCreate):
        return research_jobs.create(**body.model_dump())

    @app.get('/api/research-jobs', response_model=ResearchJobPage)
    def research_job_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                          research_id: str | None = Query(None, max_length=64)):
        return research_jobs.list(limit=limit, offset=offset, research_id=research_id)

    @app.get('/api/research-jobs/{job_id}', response_model=ResearchJob)
    def research_job_get(job_id: str):
        return research_jobs.get(job_id)

    @app.post('/api/research-jobs/{job_id}/advance', response_model=JobStep)
    def research_job_advance(job_id: str, body: ResearchJobAdvance):
        return research_jobs.advance(job_id, **body.model_dump())

    @app.get('/api/research-jobs/{job_id}/markdown', response_class=Response,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def research_job_markdown(job_id: str):
        return Response(research_jobs.markdown(job_id), media_type='text/markdown',
                        headers={'Content-Disposition': 'attachment; filename="research-execution.md"'})

    @app.get('/api/research-guards', response_model=GuardPage)
    def research_guard_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return research_guard.list(limit=limit, offset=offset)

    @app.get('/api/researches/{research_id}/temporal-guard', response_model=GuardStatus)
    def research_guard_get(research_id: str):
        return research_guard.get(research_id)

    @app.post('/api/research-claims/preview', response_model=ClaimPreview)
    def research_claim_preview(body: ClaimPreviewRequest):
        return research_claims.preview(**body.model_dump())

    @app.post('/api/research-claims', response_model=ClaimDetail, status_code=201)
    def research_claim_create(body: ClaimCreate):
        return research_claims.create(**body.model_dump())

    @app.get('/api/research-claims', response_model=ClaimPage)
    def research_claim_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                            case_id: str | None = Query(None, max_length=200)):
        return research_claims.list(limit=limit, offset=offset, case_id=case_id)

    @app.get('/api/research-claims/{claims_id}', response_model=ClaimDetail)
    def research_claim_get(claims_id: str):
        return research_claims.get(claims_id)

    @app.get('/api/research-claims/{claims_id}/markdown', response_class=Response,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def research_claim_markdown(claims_id: str):
        return Response(research_claims.markdown(claims_id), media_type='text/markdown',
                        headers={'Content-Disposition': 'attachment; filename="research-claims.md"'})

    @app.get('/api/claim-review-targets/{claims_id}', response_model=ClaimReviewTarget)
    def claim_review_target(claims_id: str, claim_id: str = Query(..., min_length=1, max_length=120)):
        return claim_reviews.target(claims_id, claim_id)

    @app.get('/api/review-materials/{claims_id}', response_model=ReviewMaterialsDetail,
             response_model_exclude_unset=True)
    def exact_review_materials(claims_id: str,
            claim_id: str = Query(..., min_length=1, max_length=120),
            expected_target_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$')):
        return review_materials.get(claims_id, claim_id, expected_target_digest)

    @app.get('/api/review-materials/{claims_id}/sources/{source_id}', response_class=Response,
             responses={200: {'description': 'Bytes of an exact-target verified allowlisted source.',
                 'content': {media: {'schema': {'type': 'string', 'format': 'binary'}}
                             for media in ('application/pdf', 'application/json', 'text/plain')}}})
    def exact_review_source(claims_id: str, source_id: str,
            claim_id: str = Query(..., min_length=1, max_length=120),
            expected_target_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$')):
        source = review_materials.source(claims_id, claim_id, expected_target_digest, source_id)
        return Response(source['content'], media_type=source['media_type'],
            headers={'Content-Disposition': f'inline; filename="{source["filename"]}"',
                     'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                     'X-Source-SHA256': source['sha256']})

    @app.get('/api/claim-reviews/status', response_model=ClaimReviewStatus)
    def claim_review_status(claims_id: str = Query(..., max_length=200), claim_id: str = Query(..., min_length=1, max_length=120)):
        return claim_reviews.status(claims_id, claim_id)

    @app.post('/api/claim-reviews/preview', response_model=ClaimReviewPreview)
    def claim_review_preview(body: ClaimReviewPreviewRequest):
        return claim_reviews.preview(**body.model_dump())

    @app.post('/api/claim-reviews', response_model=ClaimReviewDetail, status_code=201)
    def claim_review_create(body: ClaimReviewCreate):
        return claim_reviews.create(**body.model_dump())

    @app.get('/api/claim-reviews', response_model=ClaimReviewPage)
    def claim_review_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
            claims_id: str | None = Query(None, max_length=200), claim_id: str | None = Query(None, max_length=120),
            source: str | None = Query(None, max_length=20), reviewer: str | None = Query(None, max_length=120)):
        return claim_reviews.list(limit=limit, offset=offset, claims_id=claims_id, claim_id=claim_id, source=source, reviewer=reviewer)

    @app.get('/api/claim-reviews/{review_id}', response_model=ClaimReviewDetail)
    def claim_review_get(review_id: str):
        return claim_reviews.get(review_id)

    @app.get('/api/claim-reviews/{review_id}/export', response_model=ClaimReviewDetail)
    def claim_review_export(review_id: str):
        return JSONResponse(strict_loads(claim_reviews.export(review_id)),
            headers={'Content-Disposition': 'attachment; filename="claim-review.json"'})

    @app.post('/api/semantic-evaluation-sets/preview', response_model=SemanticEvaluationSetPreview)
    def semantic_set_preview(body: SemanticEvaluationSetPreviewRequest):
        return semantic_sets.preview(**body.model_dump())

    @app.post('/api/semantic-evaluation-sets', response_model=SemanticEvaluationSetDetail, status_code=201)
    def semantic_set_create(body: SemanticEvaluationSetCreate):
        return semantic_sets.create(**body.model_dump())

    @app.get('/api/semantic-evaluation-sets', response_model=SemanticEvaluationSetPage)
    def semantic_set_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return semantic_sets.list(limit=limit, offset=offset)

    @app.get('/api/semantic-evaluation-sets/{set_id}', response_model=SemanticEvaluationSetDetail)
    def semantic_set_get(set_id: str):
        return semantic_sets.get(set_id)

    @app.get('/api/semantic-evaluation-sets/{set_id}/export', response_model=SemanticEvaluationBundle)
    def semantic_set_export(set_id: str, response: Response):
        response.headers['Content-Disposition'] = 'attachment; filename="semantic-reference-bundle.json"'
        return semantic_sets.export(set_id)

    @app.get('/api/semantic-evaluation-sets/{set_id}/comparisons', response_model=SemanticEvaluationComparisonPage)
    def semantic_set_comparisons(set_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return semantic_sets.comparisons(set_id, limit=limit, offset=offset)

    @app.post('/api/semantic-evaluation-comparisons', response_model=SemanticEvaluationComparisonDetail, status_code=201)
    def semantic_comparison_create(body: SemanticEvaluationComparisonCreate):
        return semantic_sets.compare(**body.model_dump())

    @app.get('/api/semantic-evaluation-comparisons/{comparison_id}', response_model=SemanticEvaluationComparisonDetail)
    def semantic_comparison_get(comparison_id: str):
        return semantic_sets.comparison(comparison_id)

    @app.post('/api/research-bindings/prepare', response_model=BindingPreview)
    def research_binding_prepare(body: BindingPrepareRequest):
        return research_bindings.prepare(**body.model_dump())

    @app.post('/api/research-bindings/preview', response_model=BindingPreview)
    def research_binding_preview(body: BindingPreviewRequest):
        return research_bindings.preview(**body.model_dump())

    @app.post('/api/research-bindings', response_model=ResearchBinding, status_code=201)
    def research_binding_create(body: BindingCreate):
        return research_bindings.create(**body.model_dump())

    @app.get('/api/research-bindings', response_model=BindingPage)
    def research_binding_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
            study_id: str | None = Query(None, max_length=200)):
        return research_bindings.list(limit=limit, offset=offset, study_id=study_id)

    @app.get('/api/research-bindings/{binding_id}', response_model=ResearchBinding)
    def research_binding_get(binding_id: str):
        return research_bindings.get(binding_id)

    @app.get('/api/research-bindings/{binding_id}/export', response_model=BindingExport)
    def research_binding_export(binding_id: str, response: Response):
        response.headers['Content-Disposition'] = 'attachment; filename="research-binding.json"'
        return research_bindings.export(binding_id)

    @app.get('/api/research-bindings/{binding_id}/markdown', response_class=Response,
            responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def research_binding_markdown(binding_id: str):
        return Response(research_bindings.markdown(binding_id), media_type='text/markdown',
            headers={'Content-Disposition': 'attachment; filename="research-binding.md"'})

    @app.post('/api/domain-research-jobs', response_model=DomainResearchJob, status_code=201)
    def domain_job_create(body: DomainJobCreate):
        return domain_jobs.create(**body.model_dump())

    @app.get('/api/domain-research-jobs', response_model=DomainJobPage)
    def domain_job_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                        source_kind: str | None = Query(None, max_length=40)):
        return domain_jobs.list(limit=limit, offset=offset, source_kind=source_kind)

    @app.get('/api/domain-research-jobs/{job_id}', response_model=DomainResearchJob)
    def domain_job_get(job_id: str):
        return domain_jobs.get(job_id)

    @app.post('/api/domain-research-jobs/{job_id}/advance', response_model=DomainJobStep)
    def domain_job_advance(job_id: str, body: DomainJobAdvance):
        return domain_jobs.advance(job_id, **body.model_dump())

    @app.get('/api/domain-research-jobs/{job_id}/markdown', response_class=Response,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def domain_job_export(job_id: str):
        return Response(domain_jobs.markdown(job_id), media_type='text/markdown',
                        headers={'Content-Disposition': 'attachment; filename="domain-research-execution.md"'})

    @app.get('/api/semantic-materials', response_model=SemanticMaterialCollection)
    def semantic_material_list():
        return semantic_annotations.materials()

    @app.get('/api/semantic-material-sources/{source_id}', response_class=FileResponse)
    def semantic_material_source(source_id: str):
        path, media_type = semantic_annotations.source_file(source_id)
        return FileResponse(path, media_type=media_type)

    @app.get('/api/semantic-materials/{case_id}', response_model=SemanticMaterialDetail)
    def semantic_material_get(case_id: str):
        return semantic_annotations.material(case_id)

    @app.get('/api/semantic-annotations/summary', response_model=SemanticAnnotationSummary)
    def semantic_annotation_summary(reviewer: str | None = Query(None, max_length=120)):
        return semantic_annotations.summary(reviewer=reviewer)

    @app.post('/api/semantic-annotations/preview', response_model=SemanticAnnotationPreview)
    def semantic_annotation_preview(body: SemanticAnnotationPreviewRequest):
        return semantic_annotations.preview(**body.model_dump())

    @app.post('/api/semantic-annotations', response_model=SemanticAnnotationDetail, status_code=201)
    def semantic_annotation_create(body: SemanticAnnotationCreate):
        return semantic_annotations.create(**body.model_dump())

    @app.get('/api/semantic-annotations', response_model=SemanticAnnotationPage)
    def semantic_annotation_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                                  case_id: str | None = Query(None, max_length=120),
                                  source: str | None = Query(None, max_length=20),
                                  reviewer: str | None = Query(None, max_length=120)):
        return semantic_annotations.list(limit=limit, offset=offset, case_id=case_id, source=source, reviewer=reviewer)

    @app.get('/api/semantic-annotations/{annotation_id}', response_model=SemanticAnnotationDetail)
    def semantic_annotation_get(annotation_id: str):
        return semantic_annotations.get(annotation_id)

    @app.get('/api/semantic-annotations/{annotation_id}/export', response_class=Response,
             responses={200: {'content': {'application/json': {'schema': {'type': 'object'}}}}})
    def semantic_annotation_export(annotation_id: str):
        return Response(semantic_annotations.export(annotation_id), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="semantic-material-annotation.json"'})

    @app.get('/api/research-protocols/presets', response_model=ProtocolPresets)
    def research_protocol_presets():
        return {'presets': protocol_presets()}

    @app.post('/api/research-protocols/preview', response_model=ProtocolPreview)
    def research_protocol_preview(body: ProtocolPreviewCreate):
        return protocol_preview(body.config.model_dump(), body.target_month, body.bundle)

    @app.post('/api/research-protocols', response_model=ProtocolRecord, status_code=201)
    def research_protocol_create(body: ProtocolCreate):
        return protocols.create(**body.model_dump())

    @app.get('/api/research-protocols', response_model=ProtocolPage)
    def research_protocol_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return protocols.list(limit=limit, offset=offset)

    @app.get('/api/research-protocols/{protocol_id}', response_model=ProtocolRecord)
    def research_protocol_get(protocol_id: str):
        return protocols.get(protocol_id)

    @app.post('/api/author-studies', response_model=StudyDetail, status_code=201)
    def author_study_create(body: StudyCreate):
        return author_studies.create(**body.model_dump())

    @app.get('/api/author-studies', response_model=StudyPage)
    def author_study_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return author_studies.list(limit=limit, offset=offset)

    @app.get('/api/author-studies/{study_id}', response_model=StudyDetail)
    def author_study_get(study_id: str):
        return author_studies.get(study_id)

    @app.get('/api/author-studies/{study_id}/markdown', response_class=Response,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def author_study_markdown(study_id: str):
        return Response(author_studies.markdown(study_id), media_type='text/markdown',
                        headers={'Content-Disposition': 'attachment; filename="author-study-report.md"'})

    @app.post('/api/author-studies/{study_id}/reviews', response_model=StudyReview, status_code=201)
    def author_study_review(study_id: str, body: StudyReviewCreate):
        return author_studies.review(study_id, **body.model_dump())

    @app.get('/api/author-studies/{study_id}/reviews', response_model=StudyReviews)
    def author_study_reviews(study_id: str):
        return author_studies.reviews(study_id)

    @app.post('/api/author-panels', response_model=AuthorPanelDetail, status_code=201)
    def author_panel_create(body: AuthorPanelCreate):
        return author_panels.create(**body.model_dump())

    @app.get('/api/author-panels', response_model=AuthorPanelPage)
    def author_panel_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return author_panels.list(limit=limit, offset=offset)

    @app.get('/api/author-panels/{panel_id}', response_model=AuthorPanelDetail)
    def author_panel_get(panel_id: str):
        return author_panels.get(panel_id)

    @app.get('/api/author-panels/{panel_id}/markdown', response_class=Response,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def author_panel_markdown(panel_id: str):
        return Response(author_panels.markdown(panel_id), media_type='text/markdown',
                        headers={'Content-Disposition': 'attachment; filename="author-panel-diagnostic.md"'})

    @app.get('/api/monthly-experiments/defaults', response_model=monthly_responses.MonthlyDefaults)
    def monthly_defaults():
        return {'config': {'schema_version': 1, 'start_month': '2025-01', 'end_month': '2025-06',
                           'cost_bps': 0.0, 'min_assets': 18},
                'data_kind': 'controlled_fixture', 'source_id': 'fictional-monthly-portfolios-v1',
                'warnings': ['受控夹具，仅验证项目定义和软件行为。',
                             '成本、换手、净收益和净值为目标权重代理，不含持仓漂移、融资、借券和滑点。',
                             '此入口为开发实验，不开放真实数据或最终 test。']}

    @app.post('/api/monthly-experiments', response_model=monthly_responses.MonthlyAck, status_code=201)
    def monthly_create(body: monthly_responses.MonthlyCreate):
        return monthly.create(**body.model_dump())

    @app.get('/api/monthly-experiments', response_model=monthly_responses.MonthlyPage)
    def monthly_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return monthly.list(limit=limit, offset=offset)

    @app.get('/api/monthly-experiments/{experiment_id}', response_model=monthly_responses.MonthlyDetail)
    def monthly_get(experiment_id: str):
        return monthly.get(experiment_id)

    @app.get('/api/monthly-experiments/{experiment_id}/status', response_model=monthly_responses.MonthlyStatus)
    def monthly_status(experiment_id: str):
        return monthly.status(experiment_id)

    @app.post('/api/monthly-experiments/{experiment_id}/cancel', response_model=monthly_responses.MonthlyAck)
    def monthly_cancel(experiment_id: str, body: monthly_responses.MonthlyCancel):
        return monthly.cancel(experiment_id, **body.model_dump())

    @app.post('/api/monthly-experiments/{experiment_id}/retry', response_model=monthly_responses.MonthlyAck)
    def monthly_retry(experiment_id: str, body: monthly_responses.MonthlyRetry):
        return monthly.retry(experiment_id, **body.model_dump())

    @app.post('/api/monthly-experiments/{experiment_id}/reviews', response_model=monthly_responses.MonthlyReview, status_code=201)
    def monthly_review(experiment_id: str, body: monthly_responses.MonthlyReviewCreate):
        return monthly.create_review(experiment_id, **body.model_dump())

    @app.post('/api/monthly-experiments/{experiment_id}/reports', response_model=monthly_responses.MonthlyReport, status_code=201)
    def monthly_report(experiment_id: str, body: monthly_responses.MonthlyReportCreate):
        return monthly.create_report(experiment_id, **body.model_dump())

    @app.get('/api/monthly-experiments/{experiment_id}/reports/{report_id}', response_model=monthly_responses.MonthlyReport)
    def monthly_report_get(experiment_id: str, report_id: str):
        return monthly.get_report(experiment_id, report_id)

    @app.get('/api/monthly-experiments/{experiment_id}/reports/{report_id}/json', response_model=monthly_responses.MonthlyReport)
    def monthly_report_json(experiment_id: str, report_id: str, response: Response):
        response.headers['Content-Disposition'] = 'attachment; filename="monthly-report.json"'
        return monthly.get_report(experiment_id, report_id)

    @app.get('/api/monthly-experiments/{experiment_id}/reports/{report_id}/markdown', response_class=Response,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def monthly_report_markdown(experiment_id: str, report_id: str):
        return Response(monthly.get_report(experiment_id, report_id)['markdown'], media_type='text/markdown',
                        headers={'Content-Disposition': 'attachment; filename="monthly-report.md"'})

    @app.get('/api/industry-mom-sources', response_model=IndustryMomSourcePage)
    def industry_source_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return industry_sources.list(limit=limit, offset=offset)

    @app.get('/api/industry-mom-sources/{source_id}', response_model=IndustryMomSourceDetail)
    def industry_source_get(source_id: str):
        return industry_sources.get(source_id)

    @app.post('/api/industry-mom-experiments', response_model=IndustryMomAck, status_code=201)
    def industry_experiment_create(body: IndustryMomCreate):
        return industry_mom.create(**body.model_dump())

    @app.get('/api/industry-mom-experiments', response_model=IndustryMomExperimentPage)
    def industry_experiment_list(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return industry_mom.list(limit=limit, offset=offset)

    @app.get('/api/industry-mom-experiments/{experiment_id}', response_model=IndustryMomExperimentDetail)
    def industry_experiment_get(experiment_id: str):
        return industry_mom.get(experiment_id)

    @app.get('/api/industry-mom-experiments/{experiment_id}/status', response_model=IndustryMomStatus)
    def industry_experiment_status(experiment_id: str):
        return industry_mom.status(experiment_id)

    @app.post('/api/industry-mom-experiments/{experiment_id}/cancel', response_model=IndustryMomAck)
    def industry_experiment_cancel(experiment_id: str, body: IndustryMomCancel):
        return industry_mom.cancel(experiment_id, **body.model_dump())

    @app.post('/api/industry-mom-experiments/{experiment_id}/retry', response_model=IndustryMomAck)
    def industry_experiment_retry(experiment_id: str, body: IndustryMomRetry):
        return industry_mom.retry(experiment_id, **body.model_dump())

    @app.get('/api/industry-mom-experiments/{experiment_id}/report', response_class=Response,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def industry_experiment_report(experiment_id: str,
            expected_attempt_id: str = Query(..., min_length=1, max_length=200),
            expected_result_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$')):
        detail = industry_mom.get(experiment_id)
        if (detail['review_target'] != {'attempt_id': expected_attempt_id, 'result_digest': expected_result_digest}
                or detail['report_markdown'] is None):
            raise ServiceError('Industry report target changed or is unavailable; refresh the exact completed result', 409)
        return Response(detail['report_markdown'], media_type='text/markdown',
                        headers={'Content-Disposition': 'attachment; filename="industry-mom-report.md"'})

    @app.get('/api/capabilities', response_model=responses.CapabilitiesResponse, response_model_exclude_unset=True)
    def capabilities():
        return {'ai_enabled': False, 'ai_provider': None, 'proposal_mode': 'manual_or_reviewed_template',
                'data_kind': 'synthetic_only', 'data_kind_scope': 'daily_alpha_engine',
                'domains': [
                    {'domain': 'daily_alpha_engine', 'data_kind': 'synthetic_only', 'scope': 'Existing daily alpha engine uses synthetic development inputs.'},
                    {'domain': 'monthly_fixture', 'data_kind': 'controlled_fixture', 'scope': 'Existing MOM-ID monthly experiments retain their controlled fixture and cost proxy semantics.'},
                    {'domain': 'author_diagnostics', 'data_kind': 'author_aggregate_diagnostics', 'scope': 'Author aggregate diagnostics retain method/data blockers and do not acquire portfolio execution authority.'},
                    {'domain': 'industry_mom', 'data_kind': 'market_derived_portfolio_returns', 'scope': 'Only locally registered fixed industry portfolio inputs; retrospective gross-only project modification, not individual-stock paper reproduction.'},
                ], 'custom_synthetic_import': True, 'deployment': 'local_single_user',
                'execution_modes': ['agent', 'fixed', 'normalized_fixed'],
                'mode_labels': {'agent': 'Bounded deterministic repair', 'fixed': 'Fixed execution',
                                'normalized_fixed': 'Normalize aliases before fixed execution'},
                'max_run_attempts': 3, 'test_split': 'reserved', 'rag_enabled': False,
                'brain_enabled': False, 'review_scope': 'Frozen scientific contracts; literal evidence and supported numerical reference checks; economic fidelity requires human review'}

    @app.get('/api/papers', response_model=list[responses.PaperSummary], response_model_exclude_unset=True)
    def papers():
        return store.list_papers()

    @app.post('/api/papers', response_model=responses.PaperDetail, response_model_exclude_unset=True, status_code=201)
    async def upload_paper(file: UploadFile = File(...), title: str = Form(...)):
        content = await file.read(16 * 1024 * 1024 + 1)
        await file.close()
        return await run_in_threadpool(store.add_paper, content, title)

    @app.get('/api/papers/{paper_id}', response_model=responses.PaperDetail, response_model_exclude_unset=True)
    def paper(paper_id: str):
        return store.get_paper(paper_id)

    @app.get('/api/papers/{paper_id}/pdf')
    def paper_pdf(paper_id: str):
        return FileResponse(store.paper_path(paper_id), media_type='application/pdf', filename='paper.pdf')

    @app.get('/api/datasets', response_model=list[responses.DatasetResponse], response_model_exclude_unset=True)
    def datasets():
        return store.list_datasets()

    @app.get('/api/datasets/{dataset_id}', response_model=responses.DatasetDetail, response_model_exclude_unset=True)
    def dataset(dataset_id: str):
        return store.get_dataset(dataset_id)

    @app.post('/api/dataset-imports', response_model=responses.DatasetImportResponse, response_model_exclude_unset=True, status_code=201)
    async def dataset_upload(file: UploadFile = File(...), metadata: UploadFile = File(...), title: str = Form(...), idempotency_key: str = Form(...)):
        try:
            content = await file.read(16 * 1024 * 1024 + 1)
            meta = await metadata.read(2 * 1024 * 1024 + 1)
        finally:
            await file.close()
            await metadata.close()
        return await run_in_threadpool(imports.create, title, content, meta, idempotency_key)

    @app.get('/api/dataset-imports', response_model=list[responses.DatasetImportResponse], response_model_exclude_unset=True)
    def dataset_imports():
        return imports.list()

    @app.get('/api/dataset-imports/{import_id}', response_model=responses.DatasetImportResponse, response_model_exclude_unset=True)
    def dataset_import(import_id: str):
        return imports.get(import_id)

    @app.post('/api/dataset-imports/{import_id}/validate', response_model=responses.DatasetImportResponse, response_model_exclude_unset=True)
    def dataset_validate(import_id: str, body: ImportValidate):
        return imports.validate(import_id, **body.model_dump())

    @app.post('/api/dataset-imports/{import_id}/register', response_model=responses.DatasetImportResponse, response_model_exclude_unset=True)
    def dataset_register(import_id: str, body: ImportRegister):
        return imports.register(import_id, **body.model_dump())

    @app.post('/api/examples/alpha101', response_model=responses.ExampleResponse, response_model_exclude_unset=True)
    def example():
        return store.seed_example()

    @app.get('/api/researches', response_model=list[responses.ResearchSummary], response_model_exclude_unset=True)
    def researches():
        return store.list_researches()

    @app.post('/api/researches', response_model=responses.ResearchDetail, response_model_exclude_unset=True, status_code=201)
    def research_create(body: ResearchCreate):
        return store.create_research(**body.model_dump())

    @app.get('/api/researches/{research_id}', response_model=responses.ResearchDetail, response_model_exclude_unset=True)
    def research(research_id: str):
        return store.get_research(research_id)

    @app.post('/api/researches/{research_id}/revisions', response_model=responses.RevisionResponse, response_model_exclude_unset=True, status_code=201)
    def revision(research_id: str, body: RevisionCreate):
        return store.create_revision(research_id, **body.model_dump())

    @app.get('/api/runs', response_model=list[responses.RunSummary], response_model_exclude_unset=True)
    def runs():
        return store.list_runs()

    @app.post('/api/runs', response_model=responses.RunSummary, response_model_exclude_unset=True, status_code=201)
    def run_create(body: RunCreate):
        return store.submit_run(**body.model_dump())

    @app.get('/api/runs/{run_id}/status', response_model=responses.RunStatusResponse)
    def run_status(run_id: str):
        return store.get_run_status(run_id)

    @app.get('/api/runs/{run_id}', response_model=responses.RunDetail, response_model_exclude_unset=True)
    def run(run_id: str):
        return store.get_run(run_id)

    @app.post('/api/runs/{run_id}/cancel', response_model=responses.RunDetail, response_model_exclude_unset=True)
    def cancel(run_id: str):
        return store.cancel_run(run_id)

    @app.post('/api/runs/{run_id}/retry', response_model=responses.RunDetail, response_model_exclude_unset=True)
    def retry(run_id: str):
        return store.retry_run(run_id)

    @app.get('/api/runs/{run_id}/events', response_model=list[responses.EventResponse], response_model_exclude_unset=True)
    def events(run_id: str, after: int = 0):
        if after < 0:
            raise ServiceError('Event cursor must be nonnegative', 422)
        return store.events(run_id, after)

    @app.get('/api/runs/{run_id}/events/page', response_model=responses.EventPage, response_model_exclude_unset=True)
    def event_page(run_id: str, after: int = 0, limit: int = 200, through: int | None = None):
        return store.event_page(run_id, after, limit, through)

    @app.get('/api/runs/{run_id}/report')
    def report(run_id: str):
        return FileResponse(store.report_path(run_id), media_type='text/markdown', filename=f'{run_id}-report.md')

    @app.get('/api/runs/{run_id}/candidates/{candidate_id}/series', response_model=CandidateSeriesResponse)
    def get_candidate_series(run_id: str, candidate_id: str, attempt_id: str, result_digest: str):
        return candidate_series.get(run_id, candidate_id, attempt_id, result_digest)

    @app.get('/api/run-comparison', response_model=RunComparison, response_model_exclude_unset=True)
    def compare_runs(baseline_run_id: str, candidate_run_id: str):
        return insights.compare(baseline_run_id, candidate_run_id)

    @app.get('/api/workflow-observation-template', response_model=ObservationTemplate)
    def observation_template():
        return observations.template()

    @app.get('/api/researches/{research_id}/workflow-observation-context', response_model=ObservationContext)
    def workflow_observation_context(research_id: str, revision_id: str | None = None,
                                     run_id: str | None = None, attempt_id: str | None = None):
        return observation_context.get_context(research_id, revision_id=revision_id,
                                               run_id=run_id, attempt_id=attempt_id)

    @app.post('/api/researches/{research_id}/workflow-observations/validate', response_model=ObservationValidation)
    def validate_observation(research_id: str, body: ObservationValidate):
        return observations.validate(research_id, **body.model_dump())

    @app.get('/api/researches/{research_id}/workflow-observations/summary', response_model=ObservationSummary)
    def observation_summary(research_id: str):
        return observations.summary(research_id)

    @app.post('/api/researches/{research_id}/workflow-observations', response_model=ObservationDetail, status_code=201)
    def import_observation(research_id: str, body: ObservationCreate):
        return observations.import_observation(research_id, **body.model_dump())

    @app.get('/api/researches/{research_id}/workflow-observations', response_model=ObservationList)
    def list_observations(research_id: str, limit: int = 20, offset: int = 0):
        return observations.list_observations(research_id, limit=limit, offset=offset)

    @app.get('/api/researches/{research_id}/workflow-observations/{observation_id}', response_model=ObservationDetail)
    def observation(research_id: str, observation_id: str):
        return observations.get_observation(research_id, observation_id)

    @app.get('/api/researches/{research_id}/workflow-observations/{observation_id}/export', response_model=ObservationDetail)
    def export_observation(research_id: str, observation_id: str):
        content = observations.export_observation(research_id, observation_id)
        return Response(content, media_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="observation-{observation_id}.json"'})

    @app.post('/api/researches/{research_id}/reports', response_model=ResearchReport, response_model_exclude_unset=True, status_code=201)
    def create_research_report(research_id: str, body: ReportCreate):
        return insights.create_report(research_id, **body.model_dump())

    @app.get('/api/researches/{research_id}/reports', response_model=ReportList, response_model_exclude_unset=True)
    def research_reports(research_id: str, limit: int = 20, offset: int = 0):
        return insights.list_reports(research_id, limit=limit, offset=offset)

    @app.get('/api/research-reports/{report_id}', response_model=ResearchReport, response_model_exclude_unset=True)
    def research_report(report_id: str):
        return insights.get_report(report_id)

    @app.get('/api/research-reports/{report_id}/export', response_model=ResearchReport,
             responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def export_research_report(report_id: str, format: str = 'json'):
        content = insights.export_report(report_id, format)
        extension, media_type = ('json', 'application/json') if format == 'json' else ('md', 'text/markdown')
        return Response(content, media_type=media_type,
                        headers={'Content-Disposition': f'attachment; filename="research-{report_id}.{extension}"'})

    @app.get('/api/runs/{run_id}/artifacts', response_model=list[responses.ArtifactResponse], response_model_exclude_unset=True)
    def artifacts(run_id: str):
        return store.list_artifacts(run_id)

    @app.get('/api/runs/{run_id}/artifacts/{artifact_id}')
    def artifact(run_id: str, artifact_id: str):
        path, name = store.artifact_path(run_id, artifact_id)
        return FileResponse(path, filename=Path(name).name)

    @app.post('/api/runs/{run_id}/reviews', response_model=responses.ReviewResponse, response_model_exclude_unset=True, status_code=201)
    def review(run_id: str, body: ReviewCreate):
        return store.create_review(run_id, **body.model_dump())

    @app.get('/api/regression-cases', response_model=list[responses.RegressionCaseResponse], response_model_exclude_unset=True)
    def cases():
        return store.list_cases()

    @app.post('/api/regression-cases', response_model=responses.RegressionCaseResponse, response_model_exclude_unset=True, status_code=201)
    def approve(body: CaseCreate):
        return store.approve_case(**body.model_dump())

    @app.get('/api/regression-checks', response_model=list[responses.HistoricalRegressionCheck], response_model_exclude_unset=True)
    def checks():
        return store.list_checks()

    @app.post('/api/regression-checks', response_model=responses.RegressionCheckResponse, response_model_exclude_unset=True, status_code=201)
    def check(body: CheckCreate):
        return store.run_regression_check(**body.model_dump())

    @app.post('/api/issues', response_model=responses.IssueResponse, response_model_exclude_unset=True, status_code=201)
    def issue_create(body: IssueCreate):
        return feedback.create(**body.model_dump())

    @app.get('/api/issues/{issue_id}', response_model=responses.IssueResponse, response_model_exclude_unset=True)
    def issue(issue_id: str):
        return feedback.get(issue_id)

    @app.post('/api/issues/{issue_id}/events', response_model=responses.IssueResponse, response_model_exclude_unset=True, status_code=201)
    def issue_update(issue_id: str, body: IssueUpdate):
        return feedback.update(issue_id, **body.model_dump())

    @app.get('/api/catalog/{resource}', response_model=responses.CatalogPage, response_model_exclude_unset=True)
    def catalog(resource: str, after: int = 0, limit: int = 50, through: int | None = None,
                research_id: str | None = None, dataset_id: str | None = None, candidate_id: str | None = None,
                status: str | None = None, category: str | None = None, source: str | None = None):
        return feedback.page(resource, after, limit, through, research_id, status, category, source, dataset_id, candidate_id)

    @app.get('/api/feedback-summary', response_model=responses.FeedbackSummary, response_model_exclude_unset=True)
    def feedback_summary(research_id: str | None = None):
        return feedback.summary(research_id)

    @app.get('/api/feedback-summary/export')
    def feedback_export(research_id: str | None = None, format: str = 'json'):
        payload, content_type = feedback.export(research_id, format)
        return Response(payload, media_type=content_type,
                        headers={'Content-Disposition': f'attachment; filename="feedback-summary.{format}"'})

    frontend = REPO / 'frontend/dist'
    if frontend.is_dir():
        app.mount('/', StaticFiles(directory=frontend, html=True), name='frontend')
    return app
