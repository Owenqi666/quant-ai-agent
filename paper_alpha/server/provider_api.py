"""HTTP proposal boundaries: allowlisted public views and anchored artifacts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat

from fastapi import APIRouter, Query

from .provider_api_schema import (ProvidersResponse, ProposalContextResponse, ProposalSummary,
    ProposalDetail, ProposalPage, ProposalArtifactView, PublicProviderUsage, ProposalResearchReview)
from .provider_context import build_provider_context
from .provider_contract import digest as provider_digest, strict_json
from .provider_schema import ProposalCreate, ProposalHandoff
from .proposal_requests import ProposalRequests, ProposalServiceError, MAX_RECORD_BYTES
from .provider_research_review import build_research_review
from .response_schemas import ErrorResponse
from .service import ServiceError
from ..storage import digest as storage_digest


_PATH = re.compile(r'(?<![\w:/])(?:[A-Za-z]:[\\/][^\s"<>]+|/(?:Users|private|tmp|var|home|Applications|Volumes|opt|usr|etc|bin|sbin)(?:/[^\s"\'<>]+)+)')
_HTTP_URL = re.compile(r'https?://[^\s"\'<>]+', re.IGNORECASE)
_CREDENTIAL = re.compile(r'(?i)(?:sk-[a-z0-9_-]{16,}|Bearer\s+[a-z0-9_.-]+)')
_ERROR_CODES = re.compile(r'^[A-Z0-9_]{1,80}$')
_PUBLIC_CONFIG = ('config_id', 'mode', 'enabled', 'dispatchable', 'model', 'reasoning_effort',
    'expected_cli_version', 'compatibility_verified', 'tool_boundary_verified', 'inference_verified', 'readiness', 'error')


class _PublicViews:
    def __init__(self, store, registry, requests):
        self.store, self.registry, self.requests = store, registry, requests

    def text(self, value):
        if value is None:
            return None
        # Slash operators and paper URLs are research content, not paths. Only
        # explicit local roots/host executables are removed outside HTTP URLs.
        text = _CREDENTIAL.sub('[credential]', str(value))
        def local(segment):
            segment = self.store._public(segment)
            for config in self.registry.configs.values():
                executable = config.executable
                if Path(executable).is_absolute() or '\\' in executable:
                    segment = segment.replace(executable, '[local-executable]')
            return _PATH.sub('[local-path]', segment)
        parts, position = [], 0
        for match in _HTTP_URL.finditer(text):
            parts.extend((local(text[position:match.start()]), match.group()))
            position = match.end()
        parts.append(local(text[position:]))
        return ''.join(parts)

    def material(self, value):
        # Redactions are declared publicly; original exact material stays local.
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {key: self.material(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.material(item) for item in value]
        return value

    def error(self, value):
        if value is None:
            return None
        code = value.get('code', 'PROVIDER_FAILED')
        if not isinstance(code, str) or not _ERROR_CODES.fullmatch(code):
            code = 'PROVIDER_FAILED'
        return {'code': code, 'message': self.text(value.get('message', 'Provider operation failed.')),
                'retryable': bool(value.get('retryable', False))}

    def call(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except ServiceError as exc:
            error = ProposalServiceError(self.text(str(exc)), getattr(exc, 'status', 409),
                code=getattr(exc, 'code', 'PROVIDER_FAILED'), retryable=getattr(exc, 'retryable', False))
            raise error from exc

    def providers(self):
        raw = self.registry.public_status()
        items = []
        for item in raw['providers']:
            projected = {key: item[key] for key in _PUBLIC_CONFIG}
            projected['error'] = self.error(item['error'])
            if item['readiness'] is not None:
                readiness = item['readiness']
                keys = ('provider', 'configured', 'authenticated', 'inference_verified', 'tool_boundary_verified', 'cli_version', 'checked_at')
                projected['readiness'] = {**{key: readiness[key] for key in keys},
                    'errors': [self.error(error) for error in readiness['errors']]}
            items.append(self.material(projected))
        paused = bool(self.store._read("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'"))
        return {'schema_version': 1, 'allow_fixture': raw['allow_fixture'],
                'allow_real': raw.get('allow_real', False),
                'real_dispatch_enabled': raw['real_dispatch_enabled'], 'dispatch_paused': paused, 'providers': items}

    def summary(self, value):
        request, config = value['request'], value['config']
        if request.get('schema_version', 1) != 1:
            raise ProposalServiceError('Use the research-v2 API for this proposal', code='CONTRACT_ROUTE_MISMATCH')
        return {'id': value['id'], 'research_id': request['research_id'], 'base_revision_id': request['base_revision_id'],
            'provider_config_id': request['provider_config_id'], 'provider_mode': config['mode'],
            'requested_model': self.text(config['model']), 'purpose': request['purpose'], 'candidate_limit': request['candidate_limit'],
            'status': value['status'], 'created_at': value['created_at'], 'generation_deadline_at': value['deadline_at'],
            **{key: value[key] for key in ('provider_invocations', 'current_attempt_id', 'accepted_attempt_id',
                'accepted_response_digest', 'research_job_id')}, 'error': self.error(value['error'])}

    def read_file(self, value, relative, limit):
        """Reject symlink parents and validate a bounded regular-file snapshot."""
        path = self.requests._folder(value['id']) / relative
        cursor = path
        while cursor != self.store.root:
            if cursor.is_symlink():
                raise ProposalServiceError('Artifact path contains a symlink', code='ARTIFACT_INTEGRITY')
            if cursor == cursor.parent:
                raise ProposalServiceError('Artifact escaped its workspace', code='ARTIFACT_INTEGRITY')
            cursor = cursor.parent
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                    raise ValueError('Invalid artifact file')
                raw = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
                if ((before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)
                        or len(raw) != before.st_size or len(raw) > limit):
                    raise ValueError('Artifact changed while reading')
                return raw
        except (OSError, ValueError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ProposalServiceError('Registered artifact is unavailable or changed', code='ARTIFACT_INTEGRITY') from exc

    def artifacts(self, value):
        """Only anchored raw/parsed/validation/accepted materials receive IDs."""
        index = {}
        for attempt in value['attempts']:
            base = f'attempts/{attempt["id"]}'
            for suffix, kind, relative, expected in (
                ('raw', 'raw_response', base + '/response.raw.json', attempt.get('raw_sha256')),
                ('parsed', 'parsed_response', base + '/parsed.json', attempt.get('response_digest')),
                ('validation', 'validation', base + '/validation.json', attempt.get('validation'))):
                if expected is not None:
                    index[attempt['id'] + '_' + suffix] = (kind, attempt['id'], relative, expected)
        if value['accepted_response_digest'] is not None:
            index['accepted_response'] = ('accepted_response', value['accepted_attempt_id'], 'accepted/response.json', value['accepted_response_digest'])
        return index

    def artifact(self, value, artifact_id):
        item = self.artifacts(value).get(artifact_id)
        if item is None:
            raise ProposalServiceError('Artifact is not in the registered public allowlist', 404, code='ARTIFACT_NOT_FOUND')
        kind, attempt_id, relative, expected = item
        limit = value['request']['budget']['max_output_bytes'] if kind == 'raw_response' else MAX_RECORD_BYTES
        raw = self.read_file(value, relative, limit)
        source_sha256 = hashlib.sha256(raw).hexdigest()
        parsed = None
        if kind == 'raw_response':
            valid = source_sha256 == expected
        else:
            try:
                parsed = strict_json(raw, max_bytes=limit)
            except ValueError as exc:
                raise ProposalServiceError('Registered JSON artifact failed validation', code='ARTIFACT_INTEGRITY') from exc
            valid = (parsed == expected) if kind == 'validation' else provider_digest(parsed) == expected
        if not valid:
            raise ProposalServiceError('Artifact does not match its recorded digest', code='ARTIFACT_INTEGRITY')
        reference = {'id': artifact_id, 'kind': kind, 'attempt_id': attempt_id,
                     'source_bytes': len(raw), 'source_sha256': source_sha256}
        return reference, raw, parsed

    def receipt(self, receipt):
        if receipt is None:
            return None
        keys = ('started_at', 'finished_at', 'elapsed_seconds', 'exit_code', 'requested_model', 'reported_model',
                'invocation_started', 'fixture', 'cli_version', 'response_sha256', 'process_stopped')
        usage = {key: receipt['usage'][key] for key in PublicProviderUsage.model_fields}
        return self.material({**{key: receipt[key] for key in keys}, 'usage': usage})

    def detail(self, value):
        refs, materials = {}, {}
        redacted = False
        for artifact_id in self.artifacts(value):
            reference, raw, parsed = self.artifact(value, artifact_id)
            refs[artifact_id] = reference
            if parsed is not None:
                material = self.material(parsed)
                redacted |= material != parsed
                materials[artifact_id] = material
        attempts = []
        for attempt in value['attempts']:
            identity = attempt['id']
            attempts.append({'id': identity, 'sequence': attempt['sequence'], 'status': attempt['status'],
                'started_at': attempt['started_at'], 'finished_at': attempt['finished_at'], 'error': self.error(attempt['error']),
                'receipt': self.receipt(attempt['receipt']), 'output': materials.get(identity + '_parsed'),
                'validation': materials.get(identity + '_validation'),
                'artifacts': [ref for ref in refs.values() if ref['attempt_id'] == identity and ref['kind'] != 'accepted_response']})
        instruction = self.text(value['request']['instruction'])
        redacted |= instruction != value['request']['instruction']
        return {**self.summary(value), 'schema_version': 1, 'source_digest': value['context']['source_digest'],
            'context_digest': value['context_digest'], 'instruction': instruction, 'budget': value['request']['budget'],
            'accepted_response': materials.get('accepted_response'), 'attempts': attempts, 'artifacts': list(refs.values()),
            'handoff_budget': value['handoff']['request']['budget'] if value['handoff'] else None,
            'public_projection_redacted': bool(redacted), 'semantic_fidelity': 'unverified'}

    def research_review(self, identity):
        try:
            original = build_research_review(self.store, identity)
        except ServiceError:
            raise
        except (OSError, ValueError) as exc:
            raise ProposalServiceError('Review artifacts are unavailable or changed', code='REVIEW_SOURCE_INTEGRITY') from exc
        value = self.requests.get(identity)
        # The verifier closes its own frozen-source read. Do not project a newer
        # generation/handoff status onto the earlier verified review packet.
        if any(original['proposal'][key] != value[key] for key in (
                'status', 'provider_invocations', 'accepted_response_digest', 'research_job_id')):
            raise ProposalServiceError('Proposal changed during review; refresh its current state', code='REVIEW_SOURCE_CHANGED')
        detail = self.detail(value)
        evidence = lambda items: [{**item, 'normalized_start': item.get('normalized_start'),
            'normalized_end': item.get('normalized_end'), 'error': item.get('error')} for item in items]
        computation = original['computation']
        if computation is not None:
            computation = {**computation, 'missing_reason': computation.get('missing_reason')}
            candidates = []
            for candidate in computation['candidates']:
                oracle = candidate['independent_verification']
                projected = {'outcome': oracle['outcome'], 'scope': oracle.get('scope'),
                    'oracle_version': oracle.get('oracle_version'), 'reason': oracle.get('reason'),
                    'checks': [{key: check[key] for key in ('name', 'outcome', 'reason')} | {
                        'reason_code': check.get('reason_code'),
                        'details': {key: item for key, item in check.items() if key not in ('name', 'outcome', 'reason', 'reason_code')}}
                        for check in oracle.get('checks', [])]}
                candidates.append({**candidate, 'independent_verification': projected})
            computation['candidates'] = candidates
        job = original['research_job']
        job_keys = ('id', 'digest', 'state', 'stop_reason', 'base_revision_id', 'revision_id', 'run_id',
                    'candidate_checks', 'saved_result_digest', 'handoff_note', 'execution_consent_is_human_quality_review')
        packet = {'schema_version': 1, 'proposal': self.summary(value), 'identities': original['identities'],
            'paper': {key: item for key, item in original['paper'].items() if key != 'pdf_path'} | {
                'pdf_url': f'/api/papers/{original["identities"]["paper_id"]}/pdf'},
            'input_evidence': evidence(original['input_evidence']), 'input_hypotheses': original['input_hypotheses'],
            'response': original['response'], 'output_evidence': evidence(original['output_evidence']),
            'candidates': original['candidates'], 'attempts': detail['attempts'],
            'research_job': None if job is None else {key: job[key] for key in job_keys}, 'computation': computation,
            'scope': original['scope'], 'human_review': original['human_review'], 'semantic_fidelity': 'unverified',
            'source_packet_digest': original['packet_digest'],
            'public_projection_redacted': True,
            'hash_scope': 'source_packet_digest hashes the private verified packet; public_view_digest hashes this public JSON without public_view_digest.'}
        # Removing the private source path/receipt extensions is itself a public
        # projection. The private digest must not be advertised as the view hash.
        packet = self.material(packet)
        packet['public_view_digest'] = provider_digest(packet)
        return packet


def create_provider_router(store, registry) -> APIRouter:
    service = ProposalRequests(store, configs=registry.configs)
    views = _PublicViews(store, registry, service)
    router = APIRouter(prefix='/api', tags=['proposals'],
        responses={status: {'model': ErrorResponse} for status in (400, 403, 404, 409, 413, 422, 429, 500, 503)})

    def legacy(identity):
        value = views.call(service.get, identity)
        if value['request'].get('schema_version', 1) != 1:
            raise ProposalServiceError('Use the research-v2 API for this proposal', code='CONTRACT_ROUTE_MISMATCH')
        return value

    @router.get('/providers', response_model=ProvidersResponse)
    def providers():
        return views.providers()

    @router.get('/researches/{research_id}/proposal-context', response_model=ProposalContextResponse)
    def context(research_id: str, base_revision_id: str = Query(min_length=1, max_length=64)):
        original = views.call(build_provider_context, store, research_id, base_revision_id)
        public = views.material(original)
        return {'schema_version': 1, 'context_digest': provider_digest(original), 'context': public,
                'public_projection_redacted': public != original}

    @router.post('/proposals', response_model=ProposalSummary, status_code=202)
    def create(body: ProposalCreate):
        existing = views.call(service.replay_create, body)
        if existing is not None:
            return views.summary(existing)
        views.call(registry.ensure_dispatchable, body.provider_config_id)
        if store._read("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'"):
            raise ProposalServiceError('Proposal dispatch is paused; inspect its recorded recovery state', 503, code='DISPATCH_PAUSED')
        return views.summary(views.call(service.create, body))

    @router.get('/proposals', response_model=ProposalPage)
    def proposals(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0),
                  research_id: str | None = Query(default=None, max_length=64)):
        page = views.call(service.list, limit=limit, offset=offset, research_id=research_id, schema_version=1)
        return {**page, 'items': [views.summary(item) for item in page['items']]}

    @router.get('/proposals/{proposal_id}', response_model=ProposalDetail)
    def proposal(proposal_id: str):
        return views.call(views.detail, legacy(proposal_id))

    @router.get('/proposals/{proposal_id}/review', response_model=ProposalResearchReview)
    def review(proposal_id: str):
        legacy(proposal_id)
        return views.call(views.research_review, proposal_id)

    @router.post('/proposals/{proposal_id}/cancel', response_model=ProposalDetail)
    def cancel(proposal_id: str):
        legacy(proposal_id)
        return views.call(views.detail, views.call(service.cancel, proposal_id))

    @router.post('/proposals/{proposal_id}/handoff', response_model=ProposalDetail)
    def handoff(proposal_id: str, body: ProposalHandoff):
        legacy(proposal_id)
        return views.call(views.detail, views.call(service.handoff, proposal_id, body))

    @router.get('/proposals/{proposal_id}/artifacts/{artifact_id}', response_model=ProposalArtifactView)
    def artifact(proposal_id: str, artifact_id: str):
        value = legacy(proposal_id)
        reference, raw, parsed = views.call(views.artifact, value, artifact_id)
        material = {'raw_text': None, 'output': None, 'validation': None}
        if reference['kind'] == 'raw_response':
            original = raw.decode('utf-8', errors='replace')
            material['raw_text'] = views.text(original)
            redacted = material['raw_text'] != original or original.encode('utf-8') != raw
        else:
            public = views.material(parsed)
            material['validation' if reference['kind'] == 'validation' else 'output'] = public
            redacted = public != parsed
        return {'schema_version': 1, 'proposal_id': proposal_id, 'artifact': reference,
                'public_projection_redacted': redacted, 'view_sha256': provider_digest(material), **material,
                'hash_scope': 'source_sha256 hashes original file bytes; view_sha256 hashes canonical JSON of raw_text/output/validation.'}

    return router
