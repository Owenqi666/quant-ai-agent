"""Fixture-only research v2 HTTP workflow, on the original durable services."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json

from fastapi import APIRouter, Query
from fastapi.responses import Response

from ..storage import digest, json_text
from .db import connect
from .paper_materials import PaperMaterials
from .provider_api import _PublicViews
from .provider_contract import digest as wire_digest
from .provider_material_bridge import import_accepted_material
from .provider_native_transport_identity import app_server_operation_supported
from .provider_research_api_schema import (
    ResearchSourcePreviewRequest, ResearchSourcePreview, ResearchV2SessionCreate,
    PublicResearchSession, ResearchSessionPage, ResearchSessionCancel,
    ResearchSessionReconcile, PublicSessionReconciliation, ResearchV2ProposalSummary,
    ResearchV2ProposalPage, ResearchV2ProposalDetail, MaterialAdoptRequest,
    PublicMaterialVersion, PublicMaterialDetail, ResearchMaterialPage,
    MaterialEngineeringRequest, PublicMaterialEngineering, PublicMaterialDiagnostic,
    MaterialDefinitionRepairRequest, PublicMaterialDefinitionRepair)
from .provider_research_context import resolve_research_source
from .provider_research_schema import ResearchProposalCreate, CONTRACT_ID
from .proposal_requests import ProposalRequests, ProposalServiceError
from .research_session_execution import SessionExecution
from .research_sessions import ResearchSessions
from .response_schemas import ErrorResponse
from .service import ServiceError


def _session_source(descriptor, private):
    kind = descriptor['kind']
    value = {'kind': kind, 'source_digest': digest(private)}
    if kind == 'paper_selection':
        value.update(paper_id=private['paper_id'], selection_digest=private['selection_digest'],
                     source_request=deepcopy(descriptor))
    elif kind == 'accepted_material':
        value.update(material_id=private['material_id'], version_id=private['version_id'],
                     material_digest=private['material_digest'], source_request=deepcopy(descriptor))
    else:
        value.update(research_id=descriptor['research_id'], revision_id=descriptor['base_revision_id'],
                     revision_digest=private['revision']['digest'])
    return value


def _descriptor(source):
    if source['kind'] != 'research_revision':
        return source['source_request']
    return {'kind': 'research_revision', 'research_id': source['research_id'],
            'base_revision_id': source['revision_id']}


class _ResearchViews(_PublicViews):
    def call(self, operation, *args, **kwargs):
        try:
            return super().call(operation, *args, **kwargs)
        except (ValueError, OSError, KeyError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ProposalServiceError(self.text(self.store._safe_error(exc)), 422,
                                       code='RESEARCH_ARGUMENTS_INVALID') from exc

    @staticmethod
    def source(value):
        keys = ('paper_id', 'selection_digest', 'material_id', 'version_id', 'material_digest',
                'research_id', 'revision_id', 'revision_digest')
        return {'kind': value['kind'], 'source_digest': value['source_digest'],
                **{key: value.get(key) for key in keys}}

    def reservation(self, value, *, sanitize=True):
        keys = ('token', 'sequence', 'action', 'effect_key', 'action_digest', 'created_at',
                'deadline_at', 'charges', 'status', 'children', 'outcome')
        projected = {**{key: value[key] for key in keys}, 'source': self.source(value['source'])}
        return self.material(projected) if sanitize else projected

    def session(self, value):
        if value['config']['contract_id'] != CONTRACT_ID:
            raise ProposalServiceError('This session belongs to another contract', 404, code='SESSION_NOT_FOUND')
        keys = ('id', 'state_digest', 'version', 'purpose', 'config', 'created_at', 'deadline_at',
                'budget', 'usage', 'state', 'effective_state', 'stop_reason', 'remaining_seconds')
        original = {'schema_version': 2, **{key: value[key] for key in keys},
                    'root_source': self.source(value['root_source']),
                    'current_source': self.source(value['current_source']),
                    'reservations': [self.reservation(item, sanitize=False) for item in value['reservations']],
                    'semantic_fidelity': 'unverified', 'execution_scope': value['execution_scope']}
        public = self.material(original)
        return {**public, 'public_projection_redacted': public != original}

    def summary(self, value):
        request = value['request']
        if request.get('schema_version') != 2 or request.get('contract_id') != CONTRACT_ID:
            raise ProposalServiceError('This proposal belongs to another contract', 404, code='PROPOSAL_NOT_FOUND')
        return self.material({'schema_version': 2, 'id': value['id'], 'session_id': request['session_id'],
            'operation': request['operation'], 'source_kind': request['source']['kind'],
            'source_digest': request['expected_source_digest'], 'provider_config_id': request['provider_config_id'],
            'provider_mode': value['config']['mode'], 'requested_model': value['config']['model'],
            'status': value['status'], 'candidate_limit': request['candidate_limit'],
            'created_at': value['created_at'], 'generation_deadline_at': value['deadline_at'],
            **{key: value[key] for key in ('provider_invocations', 'current_attempt_id', 'accepted_attempt_id',
                                         'accepted_response_digest')}, 'error': self.error(value['error'])})

    def detail(self, value):
        summary = self.summary(value)
        refs, outputs = {}, {}
        redacted = False
        for identity, (kind, *_rest) in self.artifacts(value).items():
            if kind == 'raw_response':
                continue  # Raw bytes remain local; only strictly parsed branches are public.
            reference, _raw, parsed = self.artifact(value, identity)
            # The original artifact is verified in full before projection.
            # Native correction proofs contain private task/context material;
            # validation exposes only the existing closed public contract.
            projected = ({key: parsed[key] for key in ('valid', 'diagnostics', 'candidate_checks')}
                         if kind == 'validation' else parsed)
            public = self.material(projected)
            redacted |= public != parsed
            refs[identity], outputs[identity] = reference, public
        attempts = []
        for attempt in value['attempts']:
            identity = attempt['id']
            attempts.append({'id': identity, 'sequence': attempt['sequence'], 'status': attempt['status'],
                'started_at': attempt['started_at'], 'finished_at': attempt['finished_at'],
                'error': self.error(attempt['error']), 'receipt': self.receipt(attempt['receipt']),
                'output': outputs.get(identity + '_parsed'), 'validation': outputs.get(identity + '_validation'),
                'artifacts': [ref for ref in refs.values() if ref['attempt_id'] == identity and ref['kind'] != 'accepted_response']})
        instruction = self.text(value['request']['instruction'])
        redacted |= instruction != value['request']['instruction']
        return {**summary, 'context_digest': value['context_digest'], 'instruction': instruction,
                'budget': value['request']['budget'], 'accepted_response': outputs.get('accepted_response'),
                'attempts': attempts, 'artifacts': list(refs.values()),
                'public_projection_redacted': redacted, 'semantic_fidelity': 'unverified'}

    def version(self, value):
        keys = ('id', 'material_id', 'version', 'digest', 'draft_digest', 'parent_version_id',
                'parent_digest', 'material', 'actor', 'citation_integrity', 'semantic_fidelity', 'created_at')
        original = {key: value[key] for key in keys}
        if 'definition_repair' in value:
            original['definition_repair'] = value['definition_repair']
        original['changes'] = [{'path': item['path'], 'before_json': json_text(item['before']),
                                'after_json': json_text(item['after'])} for item in value['changes']]
        public = self.material(original)
        return {**public, 'public_projection_redacted': public != original}

    def paper_material(self, value):
        keys = ('id', 'draft_digest', 'current_digest', 'selection', 'draft', 'citation_integrity',
                'semantic_fidelity', 'created_at')
        original = {**{key: value[key] for key in keys},
                    **{key: value['source'][key] for key in ('paper_id', 'proposal_id', 'attempt_id', 'accepted_response_digest')},
                    'latest_version': self.version(value['latest_version']) if value['latest_version'] else None}
        public = self.material(original)
        nested_redacted = bool((public.get('latest_version') or {}).get('public_projection_redacted'))
        return {**public, 'public_projection_redacted': public != original or nested_redacted}


def create_research_provider_router(store, registry):
    proposals = registry.proposal_requests(store)
    # Code-owned transport choice for new extraction only; frozen requests are
    # parsed according to their saved context by all readers and workers.
    proposals.enable_material_span_references = True
    proposals.enable_grounded_material = True
    authority = proposals.research_authority
    sessions = ResearchSessions(store)
    execution = SessionExecution(store, proposals, sessions=sessions)
    materials = PaperMaterials(store)
    views = _ResearchViews(store, registry, proposals)
    router = APIRouter(prefix='/api/research-v2', tags=['research-v2'],
        responses={status: {'model': ErrorResponse} for status in (400, 403, 404, 409, 413, 422, 429, 500, 503)})

    def config_identity(config_id, *, dispatch=False, host_grant_id=None):
        config = registry.configs.get(config_id)
        if config is None:
            raise ProposalServiceError('Provider configuration is not registered', 404, code='PROVIDER_DISABLED')
        if dispatch:
            if config.mode != 'fixture' and host_grant_id is None:
                raise ProposalServiceError('Research provider v2 has no real-call qualification', 503,
                                           code='RESEARCH_V2_REAL_UNQUALIFIED')
            views.call(registry.ensure_dispatchable, config_id)
        return {'provider_config_id': config.config_id, 'provider_config_digest': wire_digest(config.model_dump()),
                'contract_id': CONTRACT_ID}

    def replay_session(body):
        with closing(connect(store.db_path)) as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT id FROM research_sessions WHERE idempotency_key=?',
                                      (body.idempotency_key,)).fetchone()
            if row is None:
                return None
            authority.verify_session_binding(connection, row['id'], body.host_grant_id)
            loaded = sessions._load(connection, row['id'])
            old = loaded['request']
            if (_descriptor(old['source']) != body.source.model_dump()
                    or old['source']['source_digest'] != body.expected_source_digest
                    or old['config'] != {'provider_config_id': body.provider_config_id,
                                         'provider_config_digest': body.expected_provider_config_digest,
                                         'contract_id': CONTRACT_ID}
                    or old['purpose'] != body.purpose or old['budget'] != body.budget.model_dump()):
                raise ProposalServiceError('Session key was used for a different plan', 409, code='IDEMPOTENCY_CONFLICT')
            return sessions._view(loaded)

    from .provider_host_schema import HostCapabilities

    @router.get('/capabilities', response_model=HostCapabilities)
    def host_capabilities():
        return views.material(views.call(authority.capabilities))

    @router.post('/source-preview', response_model=ResearchSourcePreview)
    def preview(body: ResearchSourcePreviewRequest):
        private = views.call(resolve_research_source, store, body.source.model_dump())
        source = _session_source(body.source.model_dump(), private)
        capability = (views.call(authority.inspect_source, body.host_grant_id, body.source.model_dump(), private, body.provider_config_id)
                      if body.host_grant_id else None)
        config = config_identity(body.provider_config_id)
        provider = registry.configs[body.provider_config_id]
        operations = (['extract_material'] if source['kind'] == 'paper_selection' else
                      ['generate_candidates'] if source['kind'] == 'accepted_material' else
                      ['generate_candidates', 'repair_candidate', 'decide_next_action'])
        value = {'schema_version': 2, 'contract_id': CONTRACT_ID, 'source': views.source(source),
                 'config': config,
                 'allowed_operations': [op for op in operations if app_server_operation_supported(provider, op)],
                 'real_dispatch_qualified': bool(capability and capability['real_qualified']), 'host_capability': capability,
                 'paper_selection': private.get('selection') if source['kind'] == 'paper_selection' else None}
        public = views.material(value)
        return {**public, 'public_projection_redacted': public != value}

    @router.post('/sessions', response_model=PublicResearchSession, status_code=201)
    def create_session(body: ResearchV2SessionCreate):
        existing = views.call(replay_session, body)
        if existing is not None:
            return views.session(existing)
        config = config_identity(body.provider_config_id, dispatch=True, host_grant_id=body.host_grant_id)
        if config['provider_config_digest'] != body.expected_provider_config_digest:
            raise ProposalServiceError('Provider configuration changed since preview', 409, code='SESSION_CONFIG_MISMATCH')
        private = views.call(resolve_research_source, store, body.source.model_dump())
        source = _session_source(body.source.model_dump(), private)
        if source['source_digest'] != body.expected_source_digest:
            raise ProposalServiceError('Research source changed since preview', 409, code='SOURCE_CHANGED')
        result = (views.call(authority.create_session, body, source, config) if body.host_grant_id else
                  views.call(sessions.create, source, body.purpose, config, body.budget.model_dump(), body.idempotency_key))
        return views.session(result)

    @router.get('/sessions', response_model=ResearchSessionPage)
    def list_sessions(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100000)):
        with closing(connect(store.db_path)) as connection:
            connection.execute('BEGIN')
            where = "json_extract(payload,'$.config.contract_id')=?"
            total = connection.execute('SELECT COUNT(*) FROM research_sessions WHERE ' + where, (CONTRACT_ID,)).fetchone()[0]
            rows = connection.execute('SELECT id FROM research_sessions WHERE ' + where +
                ' ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (CONTRACT_ID, limit, offset)).fetchall()
            values = [views.session(sessions._view(views.call(sessions._load, connection, row['id']))) for row in rows]
        return {'items': values, 'total': total, 'limit': limit, 'offset': offset}

    @router.get('/sessions/{session_id}', response_model=PublicResearchSession)
    def get_session(session_id: str):
        return views.session(views.call(sessions.get, session_id))

    @router.post('/sessions/{session_id}/cancel', response_model=PublicResearchSession)
    def cancel_session(session_id: str, body: ResearchSessionCancel):
        views.session(views.call(sessions.get, session_id))
        result = views.call(execution.cancel, session_id, body.expected_digest, body.idempotency_key)
        return views.session(result['session'])

    @router.post('/sessions/{session_id}/reconcile', response_model=PublicSessionReconciliation)
    def reconcile_session(session_id: str, body: ResearchSessionReconcile):
        views.session(views.call(sessions.get, session_id))
        result = views.call(execution.reconcile, session_id, body.token)
        return {'session': views.session(result['session']), 'reservation': views.reservation(result['reservation']),
                'replayed': result['replayed'], 'observation': result.get('observation')}

    @router.post('/proposals', response_model=ResearchV2ProposalSummary, status_code=202)
    def create_proposal(body: ResearchProposalCreate):
        # Restoring an acknowledged effect is allowed after permission, pause or
        # source changes; SessionExecution still checks the original ownership.
        existing = views.call(proposals.replay_create, body)
        if existing is None:
            with closing(connect(store.db_path)) as connection:
                binding = connection.execute('SELECT grant_id FROM provider_host_sessions WHERE session_id=?', (body.session_id,)).fetchone()
            config_identity(body.provider_config_id, dispatch=True, host_grant_id=binding[0] if binding else None)
            if store._read("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'"):
                raise ProposalServiceError('Proposal dispatch is paused; inspect its recovery state', 503, code='DISPATCH_PAUSED')
        result = views.call(execution.propose, body)
        return views.summary(result['proposal'])

    @router.get('/proposals', response_model=ResearchV2ProposalPage)
    def list_proposals(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100000)):
        page = views.call(proposals.list, limit=limit, offset=offset, schema_version=2)
        return {**page, 'items': [views.summary(item) for item in page['items']]}

    @router.get('/proposals/{proposal_id}', response_model=ResearchV2ProposalDetail)
    def get_proposal(proposal_id: str):
        return views.call(views.detail, views.call(proposals.get, proposal_id))

    @router.post('/proposals/{proposal_id}/material', response_model=PublicMaterialDetail, status_code=201)
    def import_material(proposal_id: str):
        return views.paper_material(views.call(import_accepted_material, store, proposal_id))

    @router.get('/materials', response_model=ResearchMaterialPage)
    def list_materials(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100000)):
        return views.material(views.call(materials.list, limit=limit, offset=offset))

    @router.get('/materials/{material_id}', response_model=PublicMaterialDetail)
    def get_material(material_id: str):
        return views.paper_material(views.call(materials.get, material_id))

    @router.get('/materials/{material_id}/versions/{version_id}', response_model=PublicMaterialVersion)
    def get_version(material_id: str, version_id: str):
        return views.version(views.call(materials.get_version, material_id, version_id))

    @router.post('/materials/{material_id}/adopt', response_model=PublicMaterialVersion, status_code=201)
    def adopt_material(material_id: str, body: MaterialAdoptRequest):
        edited = body.edited_material.model_dump() if body.edited_material is not None else None
        return views.version(views.call(materials.adopt, material_id, body.expected_digest, edited,
                                      idempotency_key=body.idempotency_key))

    @router.post('/materials/{material_id}/engineering', response_model=PublicMaterialEngineering, status_code=201)
    def configure_formula_engineering(material_id: str, body: MaterialEngineeringRequest):
        from .material_context import (ENGINEERING_CONDITIONS, build_material_projection,
                                       formula_engineering_method_policy, engineering_disposition_condition)
        current = views.call(materials.get, material_id)
        original = current['latest_version']['material'] if current['latest_version'] else current['draft']
        configured = deepcopy(original)
        dispositions = [item.model_dump() for item in body.unresolved_dispositions]
        disposition_condition = views.call(engineering_disposition_condition, dispositions)
        for hypothesis in configured['hypotheses']:
            if not all(condition in hypothesis['conditions'] for condition in ENGINEERING_CONDITIONS):
                hypothesis['conditions'].extend([*ENGINEERING_CONDITIONS, disposition_condition])
        policy = views.call(formula_engineering_method_policy, configured, dispositions)
        # Check the proposed source rules before committing the material version.
        preview = {'selection': current['selection'], 'material': configured,
                   'actor': 'automation', 'semantic_fidelity': 'unverified',
                   'material_id': material_id, 'id': 'preview', 'digest': body.expected_digest,
                   'draft_digest': current['draft_digest']}
        views.call(build_material_projection, preview, policy)
        version = views.call(materials.adopt, material_id, body.expected_digest, configured,
                             idempotency_key=body.idempotency_key, actor='automation')
        return {'version': views.version(version), 'method_policy': policy,
                'verification_scope': 'formula_engineering_only',
                'human_review_status': 'pending_human', 'human_approval': False}

    @router.post('/materials/{material_id}/definition-repair', response_model=PublicMaterialDefinitionRepair, status_code=201)
    def repair_operator_definitions(material_id: str, body: MaterialDefinitionRepairRequest):
        result = views.call(materials.repair_operator_definitions, material_id, body.expected_digest,
                            idempotency_key=body.idempotency_key)
        return {**result, 'version': views.version(result['version']) if result['version'] else None,
                'diagnostic': views.material(result['diagnostic'])}

    def material_diagnostic(material_id, dataset_id=None):
        from ..paper_research_preflight import assess
        current = views.call(materials.get, material_id)
        value = current['latest_version']['material'] if current['latest_version'] else current['draft']
        fields = None
        if dataset_id:
            metadata = json.loads(store._fetch('datasets', dataset_id)['metadata'])
            fields = [name for name in metadata.get('columns', []) if name not in {'date', 'asset'}]
        checked = views.call(assess, value, {'kind': 'daily_synthetic' if dataset_id else 'source_only'},
                             available_fields=fields, accepted=bool(current['latest_version']))
        if value.get('grounding_version') == 1:
            definition = views.call(materials.operator_definition_diagnostic, material_id)
            checked['automatic_checks']['operator_definitions'] = definition
            if definition['status'] in {'repairable', 'needs_information'}:
                for blocker in definition['blockers']:
                    if blocker['code'] not in checked['details']['blocking_reasons']:
                        checked['details']['blocking_reasons'].append(blocker['code'])
                if checked['stop_reason'] not in {'UNSUPPORTED_OPERATOR', 'FIELD_UNAVAILABLE', 'INVALID_EXPRESSION'}:
                    checked['status'] = 'needs_information'
                    checked['stop_reason'] = definition['blockers'][0]['code']
        return {'material_id': material_id, 'material_digest': current['current_digest'],
                'source': current['source'], **checked}

    @router.get('/materials/{material_id}/diagnostics', response_model=PublicMaterialDiagnostic)
    def get_material_diagnostic(material_id: str, dataset_id: str | None = None):
        return views.material(material_diagnostic(material_id, dataset_id))

    @router.get('/materials/{material_id}/report', response_class=Response,
                responses={200: {'content': {'text/markdown': {'schema': {'type': 'string'}}}}})
    def export_material_report(material_id: str, dataset_id: str | None = None):
        current = views.call(materials.get, material_id)
        checked = views.material(material_diagnostic(material_id, dataset_id))
        source = views.paper_material(current)
        text = ('# Paper research material report\n\n'
                'Human review: pending_human. Human quality, approval and time: null.\n\n'
                'Extraction: the saved proposal/attempt below identifies the actual Provider stage. '
                'These automatic source/data checks do not establish citation support or research validity.\n\n'
                'No experiment metrics are included in this material-only report. Any paper_statistics '
                'belong to the cited original paper, not a current experiment.\n\n'
                '## Saved source and material\n\n```json\n' + json.dumps(source, ensure_ascii=False, indent=2) +
                '\n```\n\n## Automatic diagnostics\n\n```json\n' + json.dumps(checked, ensure_ascii=False, indent=2) + '\n```\n')
        return Response(text, media_type='text/markdown',
                        headers={'Content-Disposition': f'attachment; filename="{material_id}-material.md"'})

    return router
