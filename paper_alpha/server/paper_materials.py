"""Immutable paper-material drafts and explicit adoption versions.

The host imports only an already verified accepted proposal packet. This module
does not grant Provider/handoff authority and never creates a research revision.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import json
import re

from ..storage import digest, json_text
from .db import connect, transaction
from .paper_segments import MaterialError, validate_material_draft, validate_selection, verify_current_selection
from .paper_materials_schema import AcceptedMaterialPacket
from .service import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_materials(
 id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers(id),
 proposal_id TEXT NOT NULL UNIQUE, attempt_id TEXT NOT NULL,
 accepted_response_digest TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
 created_at TEXT NOT NULL, metadata_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_material_versions(
 id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES paper_materials(id),
 version INTEGER NOT NULL CHECK(version>=1), parent_version_id TEXT REFERENCES paper_material_versions(id),
 payload TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL, metadata_digest TEXT NOT NULL,
 UNIQUE(material_id,version)
);
"""
MAX_BYTES = 512 * 1024
_DRAFT_KEYS = {'schema_version', 'source', 'selection', 'draft', 'citation_integrity', 'semantic_fidelity'}
_SOURCE_KEYS = {'paper_id', 'paper_sha256', 'document_digest', 'selection_digest', 'proposal_id', 'attempt_id', 'accepted_response_digest'}
_VERSION_KEYS = {'schema_version', 'material_id', 'draft_digest', 'version', 'parent_version_id', 'parent_digest', 'material', 'actor', 'changes', 'citation_integrity', 'semantic_fidelity'}


def _key(value, label='identity'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise MaterialError(f'Invalid material {label}', 422)
    return value


def _metadata(row, *, version=False):
    keys = ('id', 'material_id', 'version', 'parent_version_id', 'digest', 'created_at', 'idempotency_key', 'request_digest') if version else ('id', 'paper_id', 'proposal_id', 'attempt_id', 'accepted_response_digest', 'digest', 'created_at')
    return digest({key: row[key] for key in keys})


def _parse(payload, keys):
    if not isinstance(payload, str) or len(payload.encode()) > MAX_BYTES:
        raise MaterialError('Material payload exceeds bound', code='STORAGE_INTEGRITY')
    def pairs(items):
        value = {}
        for name, item in items:
            if name in value:
                raise MaterialError('Duplicate stored material key', code='STORAGE_INTEGRITY')
            value[name] = item
        return value
    body = json.loads(payload, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite material JSON')))
    if not isinstance(body, dict) or set(body) != keys or body.get('schema_version') != 1 or type(body.get('schema_version')) is not int or json_text(body) != payload:
        raise MaterialError('Stored material contract differs', code='STORAGE_INTEGRITY')
    return body


def _changes(before, after, path=''):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            result.extend(_changes(before.get(key), after.get(key), path + '/' + key))
        return result
    return [{'path': path, 'before': before, 'after': after}]


def _validate_edit_attribution(before, after):
    previous = {item['id']: item for item in before['hypotheses']}
    prior_evidence = {item['id']: item for item in before['evidence']}
    changed_evidence = {item['id'] for item in after['evidence'] if prior_evidence.get(item['id']) != item}
    global_change = any(before[key] != after[key] for key in ('unresolved_rules', 'limitations'))
    scientific = ('claim', 'evidence_ids', 'signal_direction', 'required_data', 'calculation', 'conditions', 'unresolved_rules')
    for item in after['hypotheses']:
        old = previous.get(item['id'])
        changed = old is None or global_change or bool(set(item['evidence_ids']) & changed_evidence) or any(old[key] != item[key] for key in scientific)
        promoted = old is not None and old['claim_attribution'] != 'paper_original' and item['claim_attribution'] == 'paper_original'
        if (changed or promoted) and item['claim_attribution'] != 'user_modification':
            raise MaterialError('Edited scientific content must be labelled user_modification; original-paper transcription corrections are not supported by this contract', 422, code='EDIT_ATTRIBUTION_REQUIRED')
        mechanism_changed = old is None or old['economic_mechanism'] != item['economic_mechanism'] or bool(set(item['evidence_ids']) & changed_evidence)
        mechanism_promoted = old is not None and old['mechanism_attribution'] != 'paper_original' and item['mechanism_attribution'] == 'paper_original'
        if (mechanism_changed or mechanism_promoted) and item['economic_mechanism'] is not None and item['mechanism_attribution'] != 'user_modification':
            raise MaterialError('Edited mechanism must be labelled user_modification', 422, code='EDIT_ATTRIBUTION_REQUIRED')


class PaperMaterials:
    def __init__(self, store, *, accepted_packet_resolver=None):
        self.store = store
        self.accepted_packet_resolver = accepted_packet_resolver

    def _accepted_packet(self, connection, source, material_digest):
        if self.accepted_packet_resolver is None:
            raise MaterialError('Material import requires an authoritative accepted-proposal ledger resolver', code='ACCEPTED_PACKET_UNVERIFIED')
        packet = AcceptedMaterialPacket.model_validate(self.accepted_packet_resolver(connection,
            proposal_id=source['proposal_id'], attempt_id=source['attempt_id'])).model_dump()
        expected = {'contract_id': 'research_provider_v2', 'operation': 'extract_material',
                    **{key: source[key] for key in ('proposal_id', 'attempt_id', 'accepted_response_digest', 'selection_digest')},
                    'material_digest': material_digest}
        if packet != expected:
            raise MaterialError('Material packet differs from authoritative accepted response', code='ACCEPTED_PACKET_UNVERIFIED')

    def _draft(self, row):
        if row is None:
            raise MaterialError('Paper material not found', 404, code='MATERIAL_NOT_FOUND')
        row = dict(row)
        body = _parse(row['payload'], _DRAFT_KEYS)
        source = body['source']
        if (not isinstance(source, dict) or set(source) != _SOURCE_KEYS or digest(body) != row['digest']
                or row['metadata_digest'] != _metadata(row) or row['id'] != 'material_' + row['digest'] or body['semantic_fidelity'] != 'unverified' or body['citation_integrity'] != 'verified'
                or any(row[key] != source[key] for key in ('paper_id', 'proposal_id', 'attempt_id', 'accepted_response_digest'))):
            raise MaterialError('Material source/identity differs', code='STORAGE_INTEGRITY')
        selection = validate_selection(body['selection'])
        if any(source[key] != selection[key] for key in ('paper_id', 'paper_sha256', 'document_digest', 'selection_digest')):
            raise MaterialError('Material source/selection differs', code='STORAGE_INTEGRITY')
        validate_material_draft(selection, body['draft'])
        return {**body, 'id': row['id'], 'draft_digest': row['digest'], 'created_at': row['created_at']}

    def _version(self, row, draft, *, connection=None, depth=0):
        if row is None:
            raise MaterialError('Material version not found', 404, code='MATERIAL_VERSION_NOT_FOUND')
        row = dict(row)
        body = _parse(row['payload'], _VERSION_KEYS)
        if (depth >= 100 or row['metadata_digest'] != _metadata(row, version=True) or digest(body) != row['digest'] or row['id'] != 'material_version_' + row['digest']
                or body['material_id'] != draft['id'] or row['material_id'] != draft['id'] or body['draft_digest'] != draft['draft_digest']
                or type(body['version']) is not int or body['version'] != row['version']
                or body['parent_version_id'] != row['parent_version_id'] or body['semantic_fidelity'] != 'unverified'
                or body['citation_integrity'] != 'verified' or body['actor'] != 'user'):
            raise MaterialError('Material version identity differs', code='STORAGE_INTEGRITY')
        validate_material_draft(draft['selection'], body['material'], accepted=True)
        if body['version'] == 1:
            if body['parent_version_id'] is not None or body['parent_digest'] != draft['draft_digest']:
                raise MaterialError('Material first-version binding differs', code='STORAGE_INTEGRITY')
            previous = draft['draft']
        else:
            if connection is None:
                raise MaterialError('Material parent verification is required', code='STORAGE_INTEGRITY')
            parent = self._version(connection.execute('SELECT * FROM paper_material_versions WHERE id=?', (body['parent_version_id'],)).fetchone(), draft, connection=connection, depth=depth + 1)
            if body['parent_digest'] != parent['digest'] or body['version'] != parent['version'] + 1:
                raise MaterialError('Material parent/version sequence differs', code='STORAGE_INTEGRITY')
            previous = parent['material']
        if body['changes'] != _changes(previous, body['material']):
            raise MaterialError('Material change record differs', code='STORAGE_INTEGRITY')
        _validate_edit_attribution(previous, body['material'])
        return {**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at'],
                'source': deepcopy(draft['source']), 'selection': deepcopy(draft['selection'])}

    def _get(self, connection, material_id):
        draft = self._draft(connection.execute('SELECT * FROM paper_materials WHERE id=?', (material_id,)).fetchone())
        latest = connection.execute('SELECT * FROM paper_material_versions WHERE material_id=? ORDER BY version DESC LIMIT 1', (material_id,)).fetchone()
        version = self._version(latest, draft, connection=connection) if latest else None
        return {**draft, 'current_digest': version['digest'] if version else draft['draft_digest'], 'latest_version': version}

    def get(self, material_id):
        _key(material_id)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return self._get(connection, material_id)

    def get_version(self, material_id, version_id):
        _key(material_id); _key(version_id)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            draft = self._draft(connection.execute('SELECT * FROM paper_materials WHERE id=?', (material_id,)).fetchone())
            return self._version(connection.execute('SELECT * FROM paper_material_versions WHERE id=?', (version_id,)).fetchone(), draft, connection=connection)

    def list(self, *, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise MaterialError('Invalid material page', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            ids = connection.execute('SELECT id FROM paper_materials ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            values = [self._get(connection, row['id']) for row in ids]
            return {'items': [{'id': value['id'], 'paper_id': value['source']['paper_id'], 'title': value['draft']['title'],
                              'draft_digest': value['draft_digest'], 'current_digest': value['current_digest'],
                              'latest_version_id': value['latest_version']['id'] if value['latest_version'] else None,
                              'semantic_fidelity': 'unverified', 'created_at': value['created_at']} for value in values],
                    'total': connection.execute('SELECT COUNT(*) FROM paper_materials').fetchone()[0], 'limit': limit, 'offset': offset}

    def create_from_proposal(self, *, proposal_id, attempt_id, accepted_response_digest, selection, draft):
        _key(proposal_id); _key(attempt_id)
        if not isinstance(accepted_response_digest, str) or not re.fullmatch('[a-f0-9]{64}', accepted_response_digest):
            raise MaterialError('Accepted response digest is required', 422)
        selection = validate_selection(selection)
        checked = validate_material_draft(selection, draft)
        source = {key: selection[key] for key in ('paper_id', 'paper_sha256', 'document_digest', 'selection_digest')}
        source.update(proposal_id=proposal_id, attempt_id=attempt_id, accepted_response_digest=accepted_response_digest)
        body = {'schema_version': 1, 'source': source, 'selection': selection, 'draft': checked['material'],
                'citation_integrity': 'verified', 'semantic_fidelity': 'unverified'}
        fingerprint = digest(body); material_id = 'material_' + fingerprint
        if len(json_text(body).encode()) > MAX_BYTES:
            raise MaterialError('Material packet exceeds bound', 413)
        # Replays acknowledge an already committed immutable effect, even if its
        # paper has subsequently changed. Only a new effect needs current bytes.
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            self._accepted_packet(connection, source, digest(checked['material']))
            existing = connection.execute('SELECT * FROM paper_materials WHERE proposal_id=?', (proposal_id,)).fetchone()
            if existing:
                prior = self._draft(existing)
                if prior['draft_digest'] != fingerprint:
                    raise MaterialError('Proposal already imported with different accepted material', code='IDEMPOTENCY_CONFLICT')
                return self._get(connection, prior['id'])
        verify_current_selection(self.store, selection)
        with transaction(self.store.db_path) as connection:
            self._accepted_packet(connection, source, digest(checked['material']))
            existing = connection.execute('SELECT * FROM paper_materials WHERE proposal_id=?', (proposal_id,)).fetchone()
            if existing:
                prior = self._draft(existing)
                if prior['draft_digest'] != fingerprint:
                    raise MaterialError('Proposal already imported with different accepted material', code='IDEMPOTENCY_CONFLICT')
                return self._get(connection, prior['id'])
            row = connection.execute('SELECT document,sha256 FROM papers WHERE id=?', (selection['paper_id'],)).fetchone()
            if not row or digest(json.loads(row['document'])) != selection['document_digest'] or row['sha256'] != selection['paper_sha256']:
                raise MaterialError('Paper changed before material commit', code='SOURCE_CHANGED')
            created = now()
            row = {'id': material_id, 'paper_id': selection['paper_id'], 'proposal_id': proposal_id, 'attempt_id': attempt_id,
                   'accepted_response_digest': accepted_response_digest, 'digest': fingerprint, 'created_at': created}
            connection.execute('INSERT INTO paper_materials VALUES (?,?,?,?,?,?,?,?,?)',
                               (material_id, selection['paper_id'], proposal_id, attempt_id, accepted_response_digest, json_text(body), fingerprint, created, _metadata(row)))
            return self._get(connection, material_id)

    def adopt(self, material_id, expected_digest, edited_material=None, *, idempotency_key, actor='user'):
        _key(material_id); _key(idempotency_key, 'idempotency key')
        if actor != 'user' or not isinstance(expected_digest, str) or not re.fullmatch('[a-f0-9]{64}', expected_digest):
            raise MaterialError('Adoption requires an explicit user and exact current digest', 422)
        request_digest = digest({'material_id': material_id, 'expected_digest': expected_digest, 'edited_material': edited_material, 'actor': actor})
        # Replay precedes source revalidation: acknowledge an existing immutable effect.
        with closing(connect(self.store.db_path)) as connection:
            receipt = connection.execute('SELECT * FROM paper_material_versions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if receipt:
                if receipt['request_digest'] != request_digest or receipt['material_id'] != material_id:
                    raise MaterialError('Adoption key was used with a different request', code='IDEMPOTENCY_CONFLICT')
                return self.get_version(material_id, receipt['id'])
        current = self.get(material_id)
        if current['current_digest'] != expected_digest:
            raise MaterialError('Material changed since review; refresh before adoption', code='MATERIAL_VERSION_CHANGED')
        verify_current_selection(self.store, current['selection'])
        previous = current['latest_version']['material'] if current['latest_version'] else current['draft']
        value = validate_material_draft(current['selection'], previous if edited_material is None else edited_material, accepted=True)['material']
        _validate_edit_attribution(previous, value)
        body = {'schema_version': 1, 'material_id': material_id, 'draft_digest': current['draft_digest'],
                'version': (current['latest_version']['version'] + 1) if current['latest_version'] else 1,
                'parent_version_id': current['latest_version']['id'] if current['latest_version'] else None,
                'parent_digest': expected_digest, 'material': value, 'actor': 'user', 'changes': _changes(previous, value),
                'citation_integrity': 'verified', 'semantic_fidelity': 'unverified'}
        if body['version'] > 100 or len(json_text(body).encode()) > MAX_BYTES:
            raise MaterialError('Material version/size limit reached', 422)
        fingerprint = digest(body); version_id = 'material_version_' + fingerprint
        with transaction(self.store.db_path) as connection:
            receipt = connection.execute('SELECT * FROM paper_material_versions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if receipt:
                if receipt['request_digest'] != request_digest or receipt['material_id'] != material_id:
                    raise MaterialError('Adoption key conflicts', code='IDEMPOTENCY_CONFLICT')
                draft = self._get(connection, material_id)
                return self._version(receipt, draft, connection=connection)
            latest = self._get(connection, material_id)
            row = connection.execute('SELECT document,sha256 FROM papers WHERE id=?', (current['source']['paper_id'],)).fetchone()
            if latest['current_digest'] != expected_digest or not row or digest(json.loads(row['document'])) != current['source']['document_digest'] or row['sha256'] != current['source']['paper_sha256']:
                raise MaterialError('Material/source changed before adoption commit', code='SOURCE_CHANGED')
            created = now()
            row = {'id': version_id, 'material_id': material_id, 'version': body['version'], 'parent_version_id': body['parent_version_id'],
                   'digest': fingerprint, 'created_at': created, 'idempotency_key': idempotency_key, 'request_digest': request_digest}
            connection.execute('INSERT INTO paper_material_versions VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (version_id, material_id, body['version'], body['parent_version_id'], json_text(body), fingerprint, created, idempotency_key, request_digest, _metadata(row, version=True)))
            return self._version(connection.execute('SELECT * FROM paper_material_versions WHERE id=?', (version_id,)).fetchone(), current, connection=connection)
