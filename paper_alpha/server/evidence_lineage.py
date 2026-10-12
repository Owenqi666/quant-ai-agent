"""Read-only exact run → adoption → original AI chunk lineage.

The saved receipt chain, rather than text similarity or the latest material,
provides identity. This module cannot create proposals, run tools or adopt data.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace

from fastapi import APIRouter, Query
from pydantic import ValidationError

from ..evidence import normalize
from ..storage import digest, json_text
from .evidence_lineage_schema import EvidenceLineageResponse
from .material_context import build_material_projection
from .paper_materials import PaperMaterials, _parse as parse_material, _VERSION_KEYS
from .provider_api import _PublicViews
from .provider_experiment_source import load_accepted_candidate
from .provider_material_bridge import _accepted_material
from .proposal_requests import ProposalRequests
from .research_jobs import ResearchJobs
from .research_session_experiments import SessionExperiments, _intent_by_resource
from .service import ServiceError, loads

MAX_RESPONSE_BYTES = 512 * 1024
MAX_ANCESTORS = 100
OFFSET_BASIS = 'normalized_pdf_page_unicode_code_points_half_open'


class LineageError(ServiceError):
    def __init__(self, message, status=409, *, code='LINEAGE_INTEGRITY'):
        super().__init__(message, status)
        self.code = code
        self.retryable = False


def _fail(message, code='LINEAGE_INTEGRITY'):
    raise LineageError(message, code=code)


def _unique(items, identity, label):
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        _fail(f'{label} records are malformed')
    selected = [item for item in items if item.get('id') == identity]
    if len(selected) != 1:
        _fail(f'{label} does not have a unique recorded identity')
    return selected[0]


def _quote(item):
    return {**deepcopy(item), 'original_quote_sha256': hashlib.sha256(item['quote'].encode()).hexdigest()}


def _excerpt(chunk, item):
    start, end = item['normalized_start'] - chunk['start'], item['normalized_end'] - chunk['start']
    if not 0 <= start < end <= len(chunk['text']) or chunk['text'][start:end] != normalize(item['quote']):
        _fail('Adopted quote does not match its exact Unicode span')
    return {'source_stage': 'adopted', 'before': chunk['text'][max(0, start - 256):start],
            'quote': chunk['text'][start:end], 'after': chunk['text'][end:end + 256],
            'bounded_context': True, 'exact_highlight': True, 'public_projection_redacted': False}


def _inheritance(original, versions, identity, key):
    # Removing and recreating a same-name item does not prove inheritance.
    if not any(item['id'] == identity for item in original[key]):
        return False
    return all(any(item['id'] == identity for item in version['material'][key]) for version in versions)


def _item_status(original, adopted, inherited):
    if not inherited:
        return 'added'
    return 'unchanged' if original == adopted else 'edited'


class EvidenceLineage:
    def __init__(self, store):
        self.store = store
        # Text-only public redaction: no provider registry/configuration startup.
        self.public = _PublicViews(store, SimpleNamespace(configs={}), None)

    def _target(self, connection, run_id, candidate_id, attempt_id, state_digest, result_digest):
        row = self.store._one(connection, 'runs', run_id)
        try:
            verification, state = loads(row['verification']), loads(row['state'])
        except (ValueError, TypeError):
            _fail('Run has no valid frozen result', 'LINEAGE_TARGET_UNAVAILABLE')
        if (row['status'] not in {'completed', 'failed'} or not isinstance(state, dict)
                or not isinstance(verification, dict) or verification.get('verified') is not True):
            _fail('A terminal verified result is required', 'LINEAGE_TARGET_UNAVAILABLE')
        if row['attempt_id'] != attempt_id or digest(state) != state_digest:
            _fail('Run target changed; refresh its exact result', 'LINEAGE_TARGET_STALE')
        candidate = _unique(state.get('candidates', []), candidate_id, 'Candidate')
        if digest(candidate) != result_digest:
            _fail('Candidate target changed; refresh its exact result', 'LINEAGE_TARGET_STALE')
        task_candidate = _unique(state['task']['candidates'], candidate_id, 'Task candidate')
        if candidate.get('hypothesis_id') != task_candidate['hypothesis_id']:
            _fail('Execution candidate differs from its Task hypothesis')
        hypothesis = _unique(state['task']['hypotheses'], task_candidate['hypothesis_id'], 'Task hypothesis')
        evidence = [_unique(state['task']['evidence'], identity, 'Task evidence') for identity in hypothesis['evidence_ids']]
        if len({item['id'] for item in evidence}) != len(evidence):
            _fail('Duplicate final hypothesis evidence')
        target = {'run_id': run_id, 'revision_id': row['revision_id'], 'candidate_id': candidate_id,
                  'attempt_id': attempt_id, 'state_digest': state_digest, 'result_digest': result_digest}
        return row, state, hypothesis, evidence, target

    @staticmethod
    def _unknown(target, hypothesis, evidence, code):
        return {'schema_version': 1, 'kind': 'run_evidence_lineage', 'target': target,
                'status': 'unknown', 'reason_code': code, 'provenance': None,
                'hypothesis': {'id': hypothesis['id'], 'status': 'unknown', 'inheritance_intact': False,
                               'original': None, 'adopted': None, 'final': deepcopy(hypothesis), 'changes': []},
                'evidence': [{'id': item['id'], 'status': 'unknown', 'reason_code': code,
                              'inheritance_intact': False, 'original': None, 'adopted': None,
                              'final': deepcopy(item), 'excerpt': None} for item in evidence], 'history': [],
                'semantic_fidelity': 'unverified', 'llm_api_called': False}

    def _owner(self, connection, row):
        # Deliberately avoid the existing helper's missing-index full-library
        # fallback. Lack of an anchor cannot be repaired by text/key guessing.
        indexed = connection.execute('SELECT 1 FROM research_session_experiment_resources WHERE kind=? AND resource_id=?',
                                     ('run', row['id'])).fetchone()
        return _intent_by_resource(self.store, connection, 'run', row['id']) if indexed else None

    def _file_marks(self, connection, row, intent, extra_paths=()):
        """Capture only bounded, exact source paths; never enumerate a library."""
        reader = ProposalRequests(self.store, configs={})
        paths = {}
        def add(path, limit=512 * 1024):
            paths[path] = limit
        base = self.store.root / 'runs' / row['id'] / 'attempts' / row['attempt_id'] / 'output'
        # Engine state/paper documents legitimately exceed proposal-record
        # limits. Keep a separate source-read bound, not the public-view limit.
        for name in ('state.json', 'inputs/paper.json'):
            add(base / name, 16 * 1024 * 1024)
        add(base / 'manifest.json', 1024 * 1024)
        add(base / 'inputs/paper.pdf', 16 * 1024 * 1024)
        for artifact in connection.execute("SELECT * FROM artifacts WHERE attempt_id=? AND name IN ('inputs/paper.pdf','inputs/paper.json')", (row['attempt_id'],)):
            path = Path(artifact['path'])
            self.store._safe_artifact_path(path, base.parent / 'exports')
            add(path, 16 * 1024 * 1024)
        research = self.store._one(connection, 'researches', row['research_id'])
        paper = self.store._one(connection, 'papers', research['paper_id'])
        add(Path(paper['pdf_path']), 16 * 1024 * 1024)
        if intent:
            plan = intent['plan']
            identities = [plan['proposal_id']]
            if plan['material']:
                identities.append(plan['material']['source']['proposal_id'])
            for identity in dict.fromkeys(identities):
                folder = reader._folder(identity)
                for name in ('request.json', 'context.json', 'provider-config.json', 'accepted/response.json'):
                    add(folder / name)
                attempts = connection.execute('SELECT id FROM proposal_attempts WHERE proposal_id=? ORDER BY sequence',
                                              (identity,)).fetchall()
                if len(attempts) > 3:
                    _fail('Proposal source exceeds its recorded attempt bound', 'LINEAGE_TOO_LARGE')
                for attempt in attempts:
                    if not re.fullmatch(r'proposal_attempt_[a-f0-9]{64}', attempt['id']):
                        _fail('Source attempt identity is malformed')
                    for name in ('prepared.json', 'receipt.json', 'response.raw.json', 'parsed.json', 'validation.json'):
                        path = folder / 'attempts' / attempt['id'] / name
                        if path.exists() or path.is_symlink():
                            add(path)
        for path, limit in extra_paths:
            self.store._safe_artifact_path(path, self.store.root)
            add(path, limit)
        if len(paths) > 64:
            _fail('Source file inventory exceeds its bound', 'LINEAGE_TOO_LARGE')
        marks, total = {}, 0
        for path, limit in paths.items():
            value = reader._read_path(path, limit)
            total += len(value)
            if total > 96 * 1024 * 1024:
                _fail('Source file bytes exceed their bound', 'LINEAGE_TOO_LARGE')
            st = path.stat()
            identity = (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            marks[path] = (limit, identity, hashlib.sha256(value).hexdigest())
        # Database source metadata also participates in the publication fence.
        return marks, {'research': dict(research), 'paper': dict(paper)}

    def _check_file_marks(self, marks):
        reader = ProposalRequests(self.store, configs={})
        for path, (limit, identity, expected_sha) in marks.items():
            try:
                value = reader._read_path(path, limit)
                st = path.stat()
                current = (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                if current != identity or hashlib.sha256(value).hexdigest() != expected_sha:
                    _fail('A source file changed while reading lineage', 'LINEAGE_SOURCE_CHANGED')
            except (OSError, ValueError) as exc:
                if isinstance(exc, LineageError):
                    raise
                raise LineageError('A source file changed while reading lineage', code='LINEAGE_SOURCE_CHANGED') from exc

    def _database_marks(self, connection, row, intent):
        """Fingerprint only rows/relationship sets read for these exact IDs."""
        marks, total = [], 0
        def add(table, column, identity, limit=1000):
            nonlocal total
            query = f'SELECT * FROM {table} WHERE {column}=? ORDER BY rowid LIMIT {limit + 1}'
            values = [dict(item) for item in connection.execute(query, (identity,))]
            total += len(json_text(values).encode())
            if len(values) > limit or total > 64 * 1024 * 1024:
                _fail('Source record lineage exceeds its read bound', 'LINEAGE_TOO_LARGE')
            marks.append((query, identity, digest(values)))
            return values
        add('revisions', 'id', row['revision_id'])
        research = self.store._one(connection, 'researches', row['research_id'])
        add('datasets', 'id', research['dataset_id'])
        add('attempts', 'id', row['attempt_id'])
        add('artifacts', 'attempt_id', row['attempt_id'])
        add('research_session_experiment_resources', 'resource_id', row['id'])
        if intent is None:
            return marks
        add('research_session_experiments', 'id', intent['id'])
        effects = add('research_session_experiment_effects', 'experiment_id', intent['id'])
        add('research_session_experiment_resources', 'experiment_id', intent['id'])
        plan = intent['plan']
        sessions = {intent['request']['session_id']}
        proposals = {plan['proposal_id']}
        if plan['material']:
            reference = plan['material']
            add('paper_materials', 'id', reference['material_id'])
            proposals.add(reference['source']['proposal_id'])
            version_id = reference['version_id']
            for _ in range(MAX_ANCESTORS):
                versions = add('paper_material_versions', 'id', version_id)
                if len(versions) != 1:
                    _fail('Source material version is missing')
                version_id = versions[0]['parent_version_id']
                if version_id is None:
                    break
            else:
                _fail('Source material ancestry exceeds its bound', 'LINEAGE_TOO_LARGE')
        for proposal_id in sorted(proposals):
            records = add('proposal_requests', 'id', proposal_id)
            if len(records) != 1:
                _fail('Source proposal is missing')
            sessions.add(json.loads(records[0]['payload'])['request']['session_id'])
            add('proposal_events', 'proposal_id', proposal_id)
            add('proposal_attempts', 'proposal_id', proposal_id)
        jobs = [json.loads(effect['payload'])['response']['id'] for effect in effects if effect['kind'] == 'job']
        for job_id in jobs:
            add('research_jobs', 'id', job_id)
            add('research_job_steps', 'job_id', job_id)
        for session_id in sorted(sessions):
            add('research_sessions', 'id', session_id)
            add('research_session_events', 'session_id', session_id, limit=3000)
            add('research_session_reservations', 'session_id', session_id, limit=3000)
        return marks

    @staticmethod
    def _check_database_marks(connection, marks):
        for query, identity, expected in marks:
            values = [dict(item) for item in connection.execute(query, (identity,))]
            if digest(values) != expected:
                _fail('Source records changed while reading lineage', 'LINEAGE_SOURCE_CHANGED')

    def _versions(self, connection, materials, draft, reference):
        version_row = connection.execute('SELECT * FROM paper_material_versions WHERE id=?', (reference['version_id'],)).fetchone()
        # The existing reader validates the whole parent/change/attribution chain
        # once. Subsequent indexed reads collect that already-verified history.
        version = materials._version(version_row, draft, connection=connection)
        if (version['digest'] != reference['material_digest'] or version['draft_digest'] != reference['draft_digest']
                or version['selection']['selection_digest'] != reference['selection_digest']
                or version['source'] != reference['source']):
            _fail('Experiment material version differs from its frozen plan')
        history, current = [], version_row
        while current is not None:
            if len(history) >= MAX_ANCESTORS:
                _fail('Material lineage exceeds its ancestor bound', 'LINEAGE_TOO_LARGE')
            body = parse_material(current['payload'], _VERSION_KEYS)
            history.append({**body, 'id': current['id'], 'digest': current['digest']})
            current = (connection.execute('SELECT * FROM paper_material_versions WHERE id=?',
                                          (body['parent_version_id'],)).fetchone() if body['parent_version_id'] else None)
        history.reverse()
        return version, history

    def _verified(self, connection, row, state, hypothesis, evidence, target, intent):
        experiments = SessionExperiments(self.store)
        run_effect = experiments._effect(connection, intent, 'run')
        job_effect = experiments._effect(connection, intent, 'job')
        if not run_effect or not job_effect:
            _fail('Run is missing its original experiment effect')
        job = ResearchJobs(self.store)._load(connection, job_effect['response']['id'])
        expected = {'revision_id': row['revision_id'], 'mode': row['mode']}
        if (run_effect['effect_key'] != row['idempotency_key'] or run_effect['request'] != expected
                or row['request_digest'] != digest(expected) or job['run_id'] != row['id']
                or job['revision_id'] != row['revision_id'] or job['research_id'] != row['research_id']
                or any(run_effect['response'][key] != row[key] for key in ('id', 'revision_id', 'research_id', 'mode'))):
            _fail('Run differs from its actual experiment/job submit receipt')
        plan = intent['plan']
        if any(state['task'].get(key) != value for key, value in plan['task'].items()):
            _fail('Final Task differs from the original experiment plan')
        packet = load_accepted_candidate(self.store, connection, plan['proposal_id'], plan['accepted_response_digest'])
        proposal = packet['proposal']
        if (digest(packet['request']) != plan['request_digest'] or proposal['context_digest'] != plan['context_digest']
                or proposal['accepted_attempt_id'] != plan['attempt_id']
                or digest(packet['output']['draft']) != plan['submitted_draft_digest']):
            _fail('Generation source differs from the experiment plan')
        if not plan['material'] or plan['source_kind'] != 'accepted_material':
            return self._unknown(target, hypothesis, evidence, 'MATERIAL_LINEAGE_NOT_RECORDED')
        reference = plan['material']
        materials = PaperMaterials(self.store)
        draft = materials._draft(connection.execute('SELECT * FROM paper_materials WHERE id=?',
                                                   (reference['material_id'],)).fetchone())
        version, ancestors = self._versions(connection, materials, draft, reference)
        accepted, selection, original = _accepted_material(self.store, connection, draft['source']['proposal_id'],
                                                          draft['source']['attempt_id'])
        if (selection != draft['selection'] or original != draft['draft']
                or accepted['accepted_response_digest'] != draft['source']['accepted_response_digest']):
            _fail('Imported material differs from the original accepted extraction')
        extraction = ProposalRequests(self.store, configs={})._load(connection, draft['source']['proposal_id'])
        adopted = build_material_projection(version)
        if any(proposal['context'].get(key) != adopted[key] or packet['output']['draft'].get(key) != adopted[key]
               or state['task'].get(key) != adopted[key] for key in ('evidence', 'hypotheses')):
            _fail('Final evidence/hypotheses differ from their exact adopted projection')
        paper = self.store._one(connection, 'papers', plan['paper_id'])
        if (paper['id'] != selection['paper_id'] or paper['sha256'] != selection['paper_sha256']
                or digest(json.loads(paper['document'])) != selection['document_digest']):
            _fail('Original selection does not bind the verified run paper')
        for final in evidence:
            proof = _unique(state['preflight']['evidence'], final['id'], 'Verified citation')
            if proof.get('paper_id') != paper['id'] or proof.get('document_sha256') != selection['paper_sha256']:
                _fail('Verified citation uses another source identity')
        chunks = {chunk['id']: chunk for chunk in selection['chunks']}
        items = []
        for final in evidence:
            adopted_e = _unique(version['material']['evidence'], final['id'], 'Adopted evidence')
            originals = [item for item in original['evidence'] if item['id'] == final['id']]
            original_e = originals[0] if originals else None
            inherited = _inheritance(original, ancestors, final['id'], 'evidence')
            status = _item_status(original_e, adopted_e, inherited)
            chunk = chunks[adopted_e['chunk_id']]
            excerpt = _excerpt(chunk, adopted_e)
            if excerpt['quote'] != normalize(final['quote']):
                _fail('Adopted exact span differs from the final evidence quote')
            if self.public.material(excerpt) != excerpt:
                excerpt.update(exact_highlight=False, public_projection_redacted=True)
            items.append({'id': final['id'], 'status': status,
                          'reason_code': 'UNCHANGED_EXTRACTION' if status == 'unchanged' else
                                         'EXPLICIT_ADOPTION_EDIT' if status == 'edited' else 'ADDED_WITHOUT_INHERITED_AI_ORIGIN',
                          'inheritance_intact': inherited,
                          'original': _quote(original_e) if original_e is not None else None,
                          'adopted': _quote(adopted_e), 'final': deepcopy(final), 'excerpt': excerpt})
        adopted_h = _unique(version['material']['hypotheses'], hypothesis['id'], 'Adopted hypothesis')
        original_h = next((item for item in original['hypotheses'] if item['id'] == hypothesis['id']), None)
        inherited_h = _inheritance(original, ancestors, hypothesis['id'], 'hypotheses')
        hstatus = _item_status(original_h, adopted_h, inherited_h)
        changed_fields = sorted('/' + key for key in adopted_h if original_h is None or original_h.get(key) != adopted_h[key])
        return {'schema_version': 1, 'kind': 'run_evidence_lineage', 'target': target, 'status': 'verified',
                'reason_code': 'RECORDED_MATERIAL_CHAIN', 'provenance': {
                    'experiment_id': intent['id'], 'generation': {'proposal_id': plan['proposal_id'],
                        'attempt_id': plan['attempt_id'], 'context_digest': plan['context_digest'],
                        'accepted_response_digest': plan['accepted_response_digest']},
                    'material': {'id': draft['id'], 'version_id': version['id'],
                        'draft_digest': draft['draft_digest'], 'material_digest': version['digest']},
                    'extraction': {'proposal_id': draft['source']['proposal_id'], 'attempt_id': draft['source']['attempt_id'],
                        'context_digest': extraction['context_digest'],
                        'accepted_response_digest': accepted['accepted_response_digest']},
                    'selection': {**{key: selection[key] for key in ('paper_id', 'paper_sha256', 'document_digest',
                        'selection_digest', 'normalization_version')}, 'offset_basis': OFFSET_BASIS,
                        'printed_page_verification': 'unverified'}},
                'hypothesis': {'id': hypothesis['id'], 'status': hstatus, 'inheritance_intact': inherited_h,
                               'original': deepcopy(original_h), 'adopted': deepcopy(adopted_h),
                               'final': deepcopy(hypothesis), 'changes': changed_fields},
                'evidence': items, 'history': [{'version_id': item['id'], 'digest': item['digest'],
                    'changed_paths': [change['path'] for change in item['changes']]} for item in ancestors],
                'semantic_fidelity': 'unverified', 'llm_api_called': False}

    def get(self, run_id, *, candidate_id, expected_attempt_id, expected_state_digest, expected_result_digest):
        return self._read(run_id, candidate_id=candidate_id, expected_attempt_id=expected_attempt_id,
                          expected_state_digest=expected_state_digest, expected_result_digest=expected_result_digest)

    def _read(self, run_id, *, candidate_id, expected_attempt_id, expected_state_digest, expected_result_digest,
              projection=None, publish=None, source_marks=None):
        """Protected read-only snapshot reusable by deterministic target projections.

        Derive private facts inside this exact verified snapshot. The same source
        publication fences run before either existing lineage or new facts leave
        the server. No later/latest state lookup is permitted by the callback.
        """
        if (not isinstance(candidate_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', candidate_id)
                or not isinstance(expected_attempt_id, str) or not 1 <= len(expected_attempt_id) <= 128
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
                       for value in (expected_state_digest, expected_result_digest))):
            raise LineageError('Complete exact lineage target parameters are required', 422, code='LINEAGE_ARGUMENTS_INVALID')
        # Explicit read-only SQLite access provides a hard boundary against GET
        # mutations. No schema initialization, authorization or lock files.
        uri = self.store.db_path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=15)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN')
            try:
                row, state, hypothesis, evidence, target = self._target(connection, run_id, candidate_id,
                    expected_attempt_id, expected_state_digest, expected_result_digest)
                intent = self._owner(connection, row)
                extra_paths = source_marks(connection, row, state, target) if source_marks is not None else ()
                marks, source_rows = self._file_marks(connection, row, intent, extra_paths)
                self.store._check_integrity(connection, row)
                if projection is not None:
                    value = projection(state, hypothesis, evidence, target)
                elif intent is None:
                    code = 'OWNERSHIP_UNAVAILABLE' if row['idempotency_key'].startswith('job-effect-') else 'MATERIAL_LINEAGE_NOT_RECORDED'
                    value = self._unknown(target, hypothesis, evidence, code)
                else:
                    value = self._verified(connection, row, state, hypothesis, evidence, target, intent)
                database_marks = self._database_marks(connection, row, intent)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                if isinstance(exc, ServiceError):
                    if exc.status == 404 and 'runs item not found' not in str(exc):
                        raise LineageError('Recorded lineage resource is missing') from exc
                    raise
                raise LineageError('Recorded lineage could not be verified') from exc
        # A fresh narrow publication fence rejects retry/source changes that
        # occurred while the read snapshot was checking filesystem artifacts.
        self._check_file_marks(marks)
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=15)) as current:
            current.row_factory = sqlite3.Row
            current.execute('BEGIN')
            self._check_database_marks(current, database_marks)
            fresh = current.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if fresh is None or dict(fresh) != row:
                _fail('Run target changed while reading lineage', 'LINEAGE_TARGET_STALE')
            for table, original_row in (('researches', source_rows['research']), ('papers', source_rows['paper'])):
                saved = current.execute(f'SELECT * FROM {table} WHERE id=?', (original_row['id'],)).fetchone()
                if saved is None or dict(saved) != original_row:
                    _fail('Source metadata changed while reading lineage', 'LINEAGE_SOURCE_CHANGED')
        return publish(value) if publish is not None else self._publish(value)

    def _publish(self, value):
        public = self.public.material(value)
        public['public_projection_redacted'] = public != value
        public['public_view_digest'] = digest(public)
        if len(json_text(public).encode()) > MAX_RESPONSE_BYTES:
            raise LineageError('Complete lineage exceeds its response bound; nothing was truncated', 413, code='LINEAGE_TOO_LARGE')
        try:
            return EvidenceLineageResponse.model_validate(public).model_dump()
        except ValidationError as exc:
            if public['public_projection_redacted'] and any(error['type'] == 'string_too_long' for error in exc.errors()):
                raise LineageError('Public redaction exceeds the bounded response schema; no lineage text was truncated',
                                   413, code='LINEAGE_PUBLIC_VIEW_TOO_LARGE') from exc
            raise LineageError('Public lineage representation failed verification') from exc


def create_evidence_lineage_router(store):
    router = APIRouter()
    lineage = EvidenceLineage(store)

    @router.get('/api/runs/{run_id}/evidence-lineage', response_model=EvidenceLineageResponse)
    def get_evidence_lineage(run_id: str,
            candidate_id: str = Query(..., pattern=r'^[A-Za-z0-9_-]{1,64}$'),
            expected_attempt_id: str = Query(..., min_length=1, max_length=128),
            expected_state_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$'),
            expected_result_digest: str = Query(..., pattern=r'^[0-9a-f]{64}$')):
        return lineage.get(run_id, candidate_id=candidate_id, expected_attempt_id=expected_attempt_id,
                           expected_state_digest=expected_state_digest, expected_result_digest=expected_result_digest)

    return router
