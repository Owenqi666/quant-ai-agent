"""Closed host resolver over the existing isolated fixture queue and files.

The anchor is produced from actual host reads, not accepted as caller-supplied
SHA declarations. It pins restart identity but is not a signature or Native
permission. No second budget ledger or original authority is modified.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat

from ..storage import digest, json_text
from .db import connect
from .provider_contract import digest as wire_digest, strict_json
from .provider_contract_registry import request_handler
from .provider_loop_host_proof_schema import HostObservedProof
from .provider_research_context import resolve_research_source
from .provider_research_targets import resolve_target
from .research_session_execution import SessionExecution
from .research_sessions import ResearchSessions

ROOT = Path(__file__).resolve().parents[2]
MAX_CODE_FILE_BYTES = 1048576
MAX_CODE_TOTAL_BYTES = 8388608
MAX_SOURCE_FILE_BYTES = 33554432
ANCHOR_VERSION = 'research-loop-host-anchor-v1'
# A code-owned list: callers cannot choose files/imports/expected hashes.
CODE_FILES = (
    'paper_alpha/expressions.py', 'paper_alpha/vendor/operators.py',
    'paper_alpha/contracts.py', 'paper_alpha/storage.py', 'paper_alpha/author_panel_schema.py',
    'paper_alpha/daily_contract.py',
    'paper_alpha/server/provider_loop_host_proof_schema.py',
    'paper_alpha/server/provider_loop_host_proof.py',
    'paper_alpha/server/provider_loop_offline_adapter.py',
    'paper_alpha/server/provider_catalog.py', 'paper_alpha/server/provider_field_semantics.py',
    'paper_alpha/server/provider_runtime.py', 'paper_alpha/server/provider_schema.py',
    'paper_alpha/server/provider_context.py', 'paper_alpha/server/material_context.py',
    'paper_alpha/server/provider_research_context.py',
    'paper_alpha/server/provider_research_targets.py',
    'paper_alpha/server/provider_repair_semantics.py',
    'paper_alpha/server/provider_research_schema.py',
    'paper_alpha/server/provider_research_contract.py',
    'paper_alpha/server/provider_contract.py', 'paper_alpha/server/provider_contract_registry.py',
    'paper_alpha/server/proposal_requests.py', 'paper_alpha/server/proposal_worker.py',
    'paper_alpha/server/research_sessions.py', 'paper_alpha/server/research_session_execution.py',
    'paper_alpha/server/research_session_context.py', 'paper_alpha/server/research_loops.py',
    'paper_alpha/server/research_session_experiments.py',
    'paper_alpha/server/provider_experiment_results.py', 'paper_alpha/server/research_jobs.py',
    'paper_alpha/server/runner.py', 'paper_alpha/workflow.py', 'paper_alpha/evaluation.py',
)


class HostProofError(ValueError):
    def __init__(self, code, message):
        super().__init__(message); self.code = code; self.retryable = False


def _require(condition, code, message):
    if not condition:
        raise HostProofError(code, message)


def _read_regular(path, root, limit):
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise HostProofError('ARTIFACT_PATH_INVALID', 'Observed file escaped host root') from exc
    _require('..' not in relative.parts and path != root, 'ARTIFACT_PATH_INVALID', 'Observed file path is invalid')
    for item in (path, *path.parents):
        _require(not item.is_symlink(), 'ARTIFACT_PATH_INVALID', 'Observed file contains a symlink')
        if item == root:
            break
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 <= before.st_size <= limit,
                     'ARTIFACT_LIMIT', 'Observed file must be a bounded regular single-link file')
            raw = stream.read(limit + 1); after = os.fstat(stream.fileno())
        _require(len(raw) == before.st_size and len(raw) <= limit
                 and (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns),
                 'ARTIFACT_CHANGED', 'Observed file changed while reading')
        return raw
    except HostProofError:
        raise
    except OSError as exc:
        raise HostProofError('ARTIFACT_UNAVAILABLE', 'Observed regular file is unavailable') from exc


def _file(role, raw):
    return {'role': role, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def code_inventory():
    files = {}; total = 0
    for name in CODE_FILES:
        raw = _read_regular(ROOT / name, ROOT, MAX_CODE_FILE_BYTES)
        total += len(raw)
        _require(total <= MAX_CODE_TOTAL_BYTES, 'CODE_LIMIT', 'Closed code inventory exceeds byte limit')
        files[name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    return {'inventory_id': 'research-loop-host-code-v1', 'files': files}


def _workspace(proposals):
    rows = proposals.store._read("SELECT value FROM settings WHERE key='provider_workspace_identity'")
    _require(len(rows) == 1, 'WORKSPACE_INVALID', 'Observed workspace identity is missing')
    return rows[0]['value']


def _anchor_identity(proposals):
    _require(proposals.configs and all(c.mode == 'fixture' and c.enabled for c in proposals.configs.values()),
             'OFFLINE_ONLY', 'Observed offline adapter accepts enabled fixture configurations only')
    return {'anchor_version': ANCHOR_VERSION, 'workspace_identity': _workspace(proposals),
            'config_wire_digests': {key: wire_digest(value.model_dump()) for key, value in proposals.configs.items()},
            'code_inventory': code_inventory()}


def _publish(path, root, value):
    path = Path(path); root = Path(root)
    _require(path.is_relative_to(root) and '..' not in path.relative_to(root).parts,
             'ARTIFACT_PATH_INVALID', 'Publication escaped host root')
    for item in (path.parent, *path.parent.parents):
        _require(not item.is_symlink(), 'ARTIFACT_PATH_INVALID', 'Publication contains a symlink')
        if item == root:
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json_text(value).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _require(_read_regular(path, root, 131072) == raw, 'ARTIFACT_CONFLICT', 'Saved host observation differs')
        return
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def initialize_host_anchor(proposals):
    """Explicit trusted-host action in a newly isolated workspace; never a grant."""
    value = _anchor_identity(proposals)
    _publish(proposals.store.root / 'host-observed-loop' / 'anchor.json', proposals.store.root, value)
    return value


class HostLoopProofResolver:
    def __init__(self, proposals):
        self.proposals = proposals
        raw = _read_regular(proposals.store.root / 'host-observed-loop' / 'anchor.json', proposals.store.root, 131072)
        self.anchor = strict_json(raw, max_bytes=131072)
        _require(self.anchor == _anchor_identity(proposals), 'HOST_ANCHOR_CHANGED', 'Actual code/config/workspace differs from sealed host anchor')

    def _check_anchor(self):
        _require(_anchor_identity(self.proposals) == self.anchor, 'HOST_ANCHOR_CHANGED', 'Host source/config/code changed since anchoring')

    def resolve_prepared(self, prepared):
        self._check_anchor()
        service = self.proposals
        try:
            with closing(connect(service.store.db_path)) as db:
                db.execute('BEGIN')
                value = service._load(db, prepared.proposal_id)
                handler = request_handler(value['request']); request = handler.parse_request(value['request'])
                _require(request.schema_version == 2 and request.source.kind == 'research_revision',
                         'SOURCE_UNSUPPORTED', 'Initial observed loop covers maintained revision fixtures only')
                _require(prepared.config.mode == 'fixture', 'OFFLINE_ONLY', 'No real Native dispatch is supported')
                _require(request.provider_config_id in service.configs and prepared.config == service.configs[request.provider_config_id]
                         and prepared.config.model_dump() == value['config'], 'CONFIG_CHANGED', 'Prepared configuration is not the host registered frozen value')
                attempt = next((a for a in value['attempts'] if a['id'] == prepared.attempt_id), None)
                _require(attempt is not None and value['current_attempt_id'] == prepared.attempt_id
                         and value['status'] == attempt['status'] == 'running', 'ATTEMPT_STALE', 'Only the active original queue attempt may be observed for dispatch')
                research = service.store._fetch('researches', request.source.research_id)
                paper = service.store._fetch('papers', research['paper_id']); dataset = service.store._fetch('datasets', research['dataset_id'])
                # Bound regular source bytes before legacy helpers hash them.
                source_reads = {role: _read_regular(path, service.store.root, MAX_SOURCE_FILE_BYTES)
                    for role, path in (('paper_pdf', paper['pdf_path']), ('market_bytes', dataset['data_path']),
                                      ('metadata_json', dataset['metadata_path']))}
                execution = SessionExecution(service.store, service)
                execution.authorize_proposal_dispatch(db, value)
                loaded = ResearchSessions(service.store)._load(db, request.session_id)
                reservation = next(r for r in loaded['reservations'] if r['effect_key'] == request.idempotency_key)
                current_state_digest = digest(loaded['state'])
                source = resolve_research_source(service.store, request.source.model_dump(), connection=db)
                _require(digest(source) == request.expected_source_digest, 'SOURCE_CHANGED', 'Actual registered source differs from request')
                fresh_context = handler.build_context(service.store, request)
                _require(fresh_context == value['context'] and wire_digest(fresh_context) == prepared.context_digest == value['context_digest'],
                         'CONTEXT_CHANGED', 'Actual exposed context differs from frozen bytes')
                expected = {'proposal_id': prepared.proposal_id, 'attempt_id': prepared.attempt_id,
                    'context_digest': prepared.context_digest, 'input_text': prepared.input_text,
                    'output_schema': prepared.output_schema, 'provider_config': prepared.config.model_dump(),
                    'budget': prepared.budget.model_dump()}
                _require(prepared.input_text == handler.build_prompt(request, fresh_context, prepared.proposal_id, prepared.attempt_id)
                         and prepared.output_schema == handler.output_schema(fresh_context)
                         and prepared.budget.model_dump() == value['request']['budget'], 'PREPARED_MISMATCH', 'Prepared prompt/schema/budget differs from actual frozen request')
                folder = service._folder(value['id']); attempt_folder = folder / 'attempts' / attempt['id']
                observed = []
                for role, path, expected_json in (
                    ('request_json', folder / 'request.json', value['request']),
                    ('context_json', folder / 'context.json', fresh_context),
                    ('config_json', folder / 'provider-config.json', value['config']),
                    ('prepared_json', attempt_folder / 'prepared.json', expected),
                ):
                    raw = service._read_path(path, 131072)
                    _require(strict_json(raw, max_bytes=131072) == expected_json, 'ARTIFACT_BINDING_MISMATCH', 'Host JSON bytes differ from their exact queue value')
                    observed.append(_file(role, raw))
                _require(digest(expected) == attempt['prepared_digest'], 'PREPARED_MISMATCH', 'Prepared storage digest differs from queue attempt')
                for role, path, expected_sha in (
                    ('paper_pdf', paper['pdf_path'], source['paper_sha256']),
                    ('market_bytes', dataset['data_path'], source['data_file_sha256']),
                    ('metadata_json', dataset['metadata_path'], source['metadata_file_sha256']),
                ):
                    raw = source_reads[role]
                    _require(_read_regular(path, service.store.root, MAX_SOURCE_FILE_BYTES) == raw, 'SOURCE_CHANGED', 'Source bytes changed during legacy verification')
                    _require(hashlib.sha256(raw).hexdigest() == expected_sha, 'SOURCE_CHANGED', 'Actual source bytes differ from registered identity')
                    observed.append(_file(role, raw))
                from .provider_catalog import catalog_from_context, loop_capability_identity
                catalog = catalog_from_context(fresh_context)
                _require(catalog['family'] == 'synthetic_daily', 'DATA_SCOPE_UNSUPPORTED', 'Real daily registration is not connected to this offline revision adapter')
                capability = loop_capability_identity(catalog, research['dataset_id'], dataset['sha256'])
                target = resolve_target(service.store, request, connection=db) if request.target is not None else None
                _require(resolve_research_source(service.store, request.source.model_dump(), connection=db) == source,
                         'SOURCE_CHANGED', 'Actual source changed during observation')
                body = {'schema_version': 1, 'proof_contract_id': 'research-loop-host-observation-v1',
                    'publisher': 'host_observed_offline', 'scope': 'isolated_fixture_host_bytes_only',
                    'native_ready': False, 'dispatch_allowed': False, 'real_model_calls': 0, 'market_numeric_rows_read': 0,
                    'business_contract_id': 'research_provider_v2', 'operation': request.operation, 'source_kind': request.source.kind,
                    'workspace_identity': _workspace(service), 'session_id': request.session_id,
                    'proposal_id': value['id'], 'attempt_id': attempt['id'], 'fencing_token': attempt['fencing_token'],
                    'reservation_token': reservation['token'], 'pre_reservation_state_digest': request.expected_session_digest,
                    'dispatch_state_digest': current_state_digest, 'request_storage_digest': digest(value['request']),
                    'source_storage_digest': digest(source), 'config_wire_digest': wire_digest(value['config']),
                    'context_wire_digest': wire_digest(fresh_context), 'prepared_storage_digest': digest(expected),
                    'anchor_storage_digest': digest(self.anchor), 'code_inventory_storage_digest': digest(self.anchor['code_inventory']),
                    'catalog_storage_digest': catalog['catalog_digest'], 'capability_storage_digest': digest(capability),
                    'dataset_id': research['dataset_id'], 'dataset_storage_digest': dataset['sha256'],
                    'target_binding_storage_digest': target['binding_digest'] if target else None, 'observed_files': observed}
            self._check_anchor()
            return HostObservedProof.model_validate({**body, 'proof_digest': digest(body)})
        except HostProofError:
            raise
        except (ValueError, KeyError, OSError, StopIteration) as exc:
            raise HostProofError(getattr(exc, 'code', 'HOST_PROOF_INVALID'), 'Actual host source/queue/target observation failed') from exc

    def publish(self, proof):
        proof = HostObservedProof.model_validate(proof)
        folder = self.proposals._folder(proof.proposal_id) / 'attempts' / proof.attempt_id
        _publish(folder / 'host-observed-proof.json', self.proposals.store.root, proof.model_dump())

    def verify_native(self, *args, **kwargs):
        raise HostProofError('RESEARCH_LOOP_NATIVE_UNQUALIFIED', 'Observed offline host bytes are not a Native execution permit')


def read_observed_proof(proposals, proposal_id, attempt_id):
    """Verify historical saved identity; preserve old integrity refusals."""
    try:
        return _read_observed_proof(proposals, proposal_id, attempt_id)
    except HostProofError:
        raise
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise HostProofError('SAVED_PROOF_MISMATCH', 'Saved observed proof or original queue artifacts failed integrity checks') from exc


def _read_observed_proof(proposals, proposal_id, attempt_id):
    value = proposals.get(proposal_id)
    attempt = next((a for a in value['attempts'] if a['id'] == attempt_id), None)
    _require(attempt is not None, 'ATTEMPT_STALE', 'Saved proof belongs to no queue attempt')
    folder = proposals._folder(proposal_id); afolder = folder / 'attempts' / attempt_id
    proof = HostObservedProof.model_validate(strict_json(proposals._read_path(afolder / 'host-observed-proof.json', 131072), max_bytes=131072))
    request = value['request']
    _require(proof.proposal_id == proposal_id and proof.attempt_id == attempt_id
             and proof.fencing_token == attempt['fencing_token'] and proof.prepared_storage_digest == attempt['prepared_digest']
             and proof.request_storage_digest == digest(request) and proof.context_wire_digest == value['context_digest']
             and proof.config_wire_digest == wire_digest(value['config']) and proof.session_id == request['session_id']
             and proof.operation == request['operation'] and proof.source_kind == request['source']['kind']
             and proof.source_storage_digest == request['expected_source_digest']
             and proof.pre_reservation_state_digest == request['expected_session_digest']
             and proof.target_binding_storage_digest == value['context'].get('target_binding_digest')
             and proof.workspace_identity == _workspace(proposals),
             'SAVED_PROOF_MISMATCH', 'Saved observed proof differs from immutable queue binding')
    with closing(connect(proposals.store.db_path)) as db:
        db.execute('BEGIN')
        session = ResearchSessions(proposals.store)._load(db, request['session_id'])
        reservations = [item for item in session['reservations'] if item['effect_key'] == request['idempotency_key']]
        _require(len(reservations) == 1 and reservations[0]['token'] == proof.reservation_token
                 and reservations[0]['action_digest'] == proof.request_storage_digest
                 and any(digest(item['state_after']) == proof.dispatch_state_digest for item in session['events']),
                 'SAVED_PROOF_MISMATCH', 'Saved observation has no original reservation and dispatch state anchor')
    anchor = strict_json(proposals._read_path(proposals.store.root / 'host-observed-loop' / 'anchor.json', 131072), max_bytes=131072)
    _require(digest(anchor) == proof.anchor_storage_digest and digest(anchor['code_inventory']) == proof.code_inventory_storage_digest,
             'SAVED_PROOF_MISMATCH', 'Saved observation differs from original host anchor')
    from .provider_catalog import catalog_from_context, loop_capability_identity
    catalog = catalog_from_context(value['context'])
    research = proposals.store._fetch('researches', request['source']['research_id'])
    dataset = proposals.store._fetch('datasets', research['dataset_id'])
    capability = loop_capability_identity(catalog, research['dataset_id'], dataset['sha256'])
    _require(proof.catalog_storage_digest == catalog['catalog_digest'] and proof.capability_storage_digest == digest(capability)
             and proof.dataset_id == research['dataset_id'] and proof.dataset_storage_digest == dataset['sha256'],
             'SAVED_PROOF_MISMATCH', 'Saved observation differs from frozen full catalog and registered dataset')
    roles = {v.role: v for v in proof.observed_files}
    for role, path in (('request_json', folder / 'request.json'), ('context_json', folder / 'context.json'),
                       ('config_json', folder / 'provider-config.json'), ('prepared_json', afolder / 'prepared.json')):
        raw = proposals._read_path(path, 131072)
        _require(role in roles and _file(role, raw) == roles[role].model_dump(), 'SAVED_PROOF_MISMATCH', 'Observed JSON file bytes changed')
    return proof
