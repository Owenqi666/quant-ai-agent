"""HTTP control of bounded host effects; workers retain queue ownership."""
from contextlib import closing

from fastapi import APIRouter, Query
from ..storage import digest
from .proposal_requests import ProposalRequests
from .provider_research_api import _ResearchViews
from .provider_loop_api_schema import PublicResearchLoop, LoopAdvance
from .research_loops import ResearchLoops, MAX_ACTIONS
from .provider_correction_public_schema import (
    LoopCreateRequest, PublicLoopResponse, PublicResearchLoopPage,
    LoopSourceSupportRequest, LoopSourceSupport,
)
from .provider_correction_public_projection import project_public_loop_v2
from .research_correction_policy import get_policy, POLICY_IDS
from .provider_research_context import resolve_research_source
from .provider_contract import digest as wire_digest
from .proposal_requests import ProposalServiceError
from .research_sessions import _parse
from .db import connect
from ..evaluation import FIELDS, AVAILABILITY
from .response_schemas import ErrorResponse


def create_loop_router(store, registry, native_start_catalog=None):
    proposals = registry.proposal_requests(store)
    service = ResearchLoops(store, proposals)
    views = _ResearchViews(store, registry, proposals)
    from .native_correction_start import ManagedNativeStarts
    from .provider_loop_native_authority import LoopNativeResearchAuthority
    managed = ManagedNativeStarts(store, registry, native_start_catalog)
    router = APIRouter(prefix='/api/research-v2/loops', tags=['research-loops'],
        responses={code: {'model': ErrorResponse} for code in (400, 403, 404, 409, 413, 422, 429, 500, 503)})

    def public(value):
        if value['schema_version'] == 2:
            return project_public_loop_v2(value,
                public_proposals=[views.summary(child) for child in value['proposals']],
                public_session=views.session(value['session']), redact=views.material)
        policy = value['request']
        experiment = value['experiment']
        job = experiment['job'] if experiment else None
        decision = None
        decision_redacted = False
        for child in reversed(value['proposals']):
            if child['request']['operation'] == 'decide_next_action' and child['status'] == 'ready':
                detail = views.detail(child)
                decision = detail['accepted_response']['decision']
                decision_redacted = detail['public_projection_redacted']
                break
        original = {key: value[key] for key in ('schema_version', 'id', 'session_id',
            'created_at', 'request_digest', 'state', 'reason', 'next_action')}
        original.update(policy={**{key: policy[key] for key in ('max_repairs', 'allow_experiment', 'experiment_budget')},
                'max_experiments': 1, 'max_actions': MAX_ACTIONS},
            actions=[{'sequence': action['sequence'], 'idempotency_key': action['idempotency_key'],
                'kind': action['kind'], 'planned_at': action['planned_at'],
                'experiment_action': action['request']['action'] if action['kind'] == 'experiment.advance' else None,
                'status': 'pending' if action['receipt'] is None else 'acknowledged',
                'request_digest': digest(action['request']),
                'receipt_digest': digest(action['receipt']) if action['receipt'] is not None else None,
                **{key: (action['receipt'] or {}).get(key) for key in ('proposal_id', 'experiment_id', 'step_id')}}
                for action in value['actions']],
            proposals=[views.summary(child) for child in value['proposals']],
            experiment=({'id': experiment['id'], 'proposal_id': experiment['request']['proposal_id'],
                'accepted_response_digest': experiment['request']['accepted_response_digest'],
                'task_digest': experiment['plan']['task_digest'], 'job_id': job['id'] if job else None,
                **{key: job.get(key) if job else None for key in ('state', 'revision_id', 'run_id')}} if experiment else None),
            decision=decision, decision_applied=any(action['kind'] == 'decision.apply' and action['receipt'] is not None
                for action in value['actions']), session=views.session(value['session']),
            semantic_fidelity='unverified', real_dispatch_qualified=False)
        projected = views.material(original)
        return {**projected, 'public_projection_redacted': projected != original
                or projected['session']['public_projection_redacted'] or decision_redacted}

    def source_qualification(research_id, revision_id):
        # Read only original DB metadata before source/PDF/data resources. This
        # mirrors N2's closed qualification; typed/real data cannot reach the
        # synthetic reader because of a browser request or display flag.
        research = store._fetch('researches', research_id)
        revision = store._revision(store._fetch('revisions', revision_id))
        if revision['research_id'] != research_id or digest(revision['task']) != revision['digest']:
            raise ProposalServiceError('Research revision failed verification', 409, code='CORRECTION_SOURCE_INTEGRITY')
        dataset = store._fetch('datasets', research['dataset_id'])
        try:
            metadata = _parse(dataset['metadata'], 1024 * 1024)
            task = revision['task']
            if type(metadata) is not dict or type(task) is not dict:
                raise ValueError('Source metadata/task must be objects')
        except (ValueError, TypeError) as exc:
            raise ProposalServiceError('Research source metadata failed verification', 409,
                                       code='CORRECTION_SOURCE_INTEGRITY') from exc
        if (type(task.get('schema_version', 1)) is not int or task.get('schema_version', 1) != 1
                or metadata.get('data_kind') != 'synthetic'
                or metadata.get('columns') != ['date', 'asset', *FIELDS]
                or metadata.get('field_availability') != AVAILABILITY):
            return False, 'CORRECTION_SOURCE_UNQUALIFIED', research
        return True, 'CORRECTION_SOURCE_SUPPORTED', research

    @router.post('/support', response_model=LoopSourceSupport)
    def support(body: LoopSourceSupportRequest):
        policy_ids = list(POLICY_IDS)
        # Actual host descriptors are read-only; these references neither
        # create grants nor mark a native invocation as qualified.
        references = []
        for identity in policy_ids:
            descriptor = get_policy(identity)
            references.append({key: descriptor[key] for key in ('policy_id', 'policy_digest')})
        value = {'schema_version': 2, 'research_id': body.research_id, 'base_revision_id': body.base_revision_id,
            'source_digest': None, 'correction_supported': False, 'support_status': 'unavailable',
            'reason_code': 'CORRECTION_SOURCE_UNAVAILABLE', 'message': 'Research source is unavailable.',
            'accepted_source_kind': 'research_revision', 'accepted_task_schema_version': 1,
            'accepted_data_kind': 'synthetic', 'candidate_limit': 1, 'max_deterministic_passes': 1,
            'correction_modes': ['deterministic'], 'policy_ids': policy_ids, 'policy_references': references,
            'provider_generation_capability': None, 'provider_config_digest': None, 'generation_available': False,
            'provider_optional_qualified': False, 'real_dispatch_qualified': False}
        try:
            qualified, code, research = source_qualification(body.research_id, body.base_revision_id)
            if not qualified:
                value.update(support_status='unsupported', reason_code=code,
                    message='This correction version supports only the maintained ordinary Task on synthetic OHLCV data.')
            elif research['latest_revision_id'] != body.base_revision_id:
                value.update(support_status='unavailable', reason_code='BASE_REVISION_CHANGED',
                    message='Select the current research revision for a new loop; historical loops remain separately readable.')
            else:
                descriptor = {'kind': 'research_revision', 'research_id': body.research_id,
                              'base_revision_id': body.base_revision_id}
                source = resolve_research_source(store, descriptor)
                value.update(source_digest=digest(source), correction_supported=True, support_status='supported',
                    reason_code='CORRECTION_SOURCE_SUPPORTED', message='One candidate and at most one deterministic representation correction; paper fidelity remains unverified.')
                config = registry.configs.get(body.provider_config_id)
                if config is None:
                    raise ProposalServiceError('Provider is not configured and enabled.', 503, code='PROVIDER_DISABLED')
                value['provider_config_digest'] = wire_digest(config.model_dump())
                registry.ensure_dispatchable(body.provider_config_id)
                if store._read("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'"):
                    raise ProposalServiceError('Provider dispatch is paused.', 503, code='DISPATCH_PAUSED')
                # Existing HTTP Host grants support material workflows, not a
                # research_revision Native root. Never treat one as a loop grant.
                if config.mode != 'fixture' or body.host_grant_id is not None:
                    raise ProposalServiceError('This public research revision entry has no compatible Native generation grant.',
                                               503, code='RESEARCH_V2_REAL_UNQUALIFIED')
                value.update(generation_available=True, message='Explicit fixture provider is configured for a synthetic demonstration; no real model or paper-fidelity qualification.')
        except (ValueError, OSError) as exc:
            value.update(reason_code=getattr(exc, 'code', 'CORRECTION_SOURCE_UNAVAILABLE'), message=str(exc))
        projected = views.material(value)
        return {**projected, 'public_projection_redacted': projected != value}

    @router.get('', response_model=PublicResearchLoopPage)
    def list_loops(research_id: str = Query(min_length=1, max_length=128),
                   base_revision_id: str | None = Query(default=None, min_length=1, max_length=128),
                   limit: int = Query(default=20, ge=1, le=50),
                   offset: int = Query(default=0, ge=0, le=10000)):
        # Filtering never skips damaged selected records: each returned item
        # must pass the original guarded service and the closed projection.
        views.call(store._fetch, 'researches', research_id)
        where = "json_extract(payload,'$.initial_proposal.source.research_id')=?"
        params = [research_id]
        if base_revision_id is not None:
            where += " AND json_extract(payload,'$.initial_proposal.source.base_revision_id')=?"
            params.append(base_revision_id)
        with closing(connect(store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT count(*) FROM research_loops WHERE ' + where, params).fetchone()[0]
            rows = connection.execute('SELECT id FROM research_loops WHERE ' + where +
                ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
        items = [views.call(public, views.call(service.get, row['id'])) for row in rows]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    @router.post('', response_model=PublicLoopResponse, status_code=201)
    def create(body: LoopCreateRequest):
        if body.schema_version == 2:
            # New requests must not enter an unqualified source reader. A replay
            # still uses the original service ledger and does not need a new
            # provider grant or Session; metadata verification grants neither.
            source = body.initial_proposal.source
            qualified, code, _ = views.call(source_qualification, source.research_id, source.base_revision_id)
            if not qualified:
                raise ProposalServiceError('Correction source is not qualified for this contract', 409, code=code)
        return views.call(public, views.call(service.create, body))

    @router.get('/{loop_id}', response_model=PublicLoopResponse)
    def get(loop_id: str):
        return views.call(public, views.call(service.get, loop_id))

    @router.post('/{loop_id}/advance', response_model=PublicLoopResponse)
    def advance(loop_id: str, body: LoopAdvance):
        # Native HTTP controls can only spend a task explicitly enabled in this
        # host catalog. An acknowledged original key is a pure saved-loop read;
        # pending keys never inherit this exception or bypass qualification.
        with closing(connect(store.db_path)) as connection:
            loaded = views.call(service._load, connection, loop_id)
            leaf = views.call(proposals.research_authority.leaf_for_session,
                              loaded['row']['session_id'], connection=connection)
            if type(leaf) is LoopNativeResearchAuthority:
                acknowledged = any(action['idempotency_key'] == body.idempotency_key
                                   and action['receipt'] is not None for action in loaded['actions'])
                if acknowledged:
                    return views.call(public, views.call(service.get, loop_id))
                views.call(managed._files)
                matches = [start_id for start_id, plan in managed.entries.items() if plan['loop_id'] == loop_id]
                if len(matches) != 1:
                    raise ProposalServiceError('Native loop is not uniquely in this host managed catalog', 409,
                                               code='NATIVE_START_NOT_FOUND')
                summary = views.call(managed.inspect, matches[0])
                if not summary['available']:
                    raise ProposalServiceError(summary['message'], 409, code=summary['reason_code'])
        return views.call(public, views.call(service.advance, loop_id, body.idempotency_key))

    @router.post('/{loop_id}/cancel', response_model=PublicLoopResponse)
    def cancel(loop_id: str, body: LoopAdvance):
        return views.call(public, views.call(service.cancel, loop_id, body.idempotency_key))

    return router


def create_native_correction_router(store, registry, catalog=None):
    """Read/attach a host-prepared task; no browser permission creator."""
    from .native_correction_start import ManagedNativeStarts
    from .native_correction_start_schema import (NativeCorrectionIdentity, NativeCorrectionSupport,
        NativeCorrectionStartRequest, NativeCorrectionStartResponse)
    managed = ManagedNativeStarts(store, registry, catalog)
    views = _ResearchViews(store, registry, managed.proposals)
    router = APIRouter(prefix='/api/research-v2/native-correction', tags=['native-correction'],
        responses={code: {'model': ErrorResponse} for code in (400, 403, 404, 409, 413, 422, 500, 503)})

    @router.post('/support', response_model=NativeCorrectionSupport)
    def native_support(body: NativeCorrectionIdentity):
        value = managed.support(body)
        projected = views.material(value)
        return {**projected, 'public_projection_redacted': projected != value}

    @router.post('/start', response_model=NativeCorrectionStartResponse)
    def native_start(body: NativeCorrectionStartRequest):
        summary, value = views.call(managed.start, body)
        loop = project_public_loop_v2(value,
            public_proposals=[views.summary(child) for child in value['proposals']],
            public_session=views.session(value['session']), redact=views.material)
        return {'schema_version': 1, 'start_id': body.start_id, 'start_digest': body.start_digest,
            'idempotency_key': body.idempotency_key, 'attached_existing': True,
            'plan': views.material(summary), 'loop': loop}

    return router
