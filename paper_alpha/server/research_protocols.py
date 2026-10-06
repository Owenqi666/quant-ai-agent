"""Immutable research rules, independent of daily Alpha101 tasks and runs.

The body, including server-supplied source statements, is content addressed.
Creation serializes only bounded JSON validation and one insert; it performs no
paper, market-data, or output-file work while holding the SQLite writer lock.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime
import json
import re

from .db import connect, transaction
from .service import ServiceError, now
from ..storage import digest, json_text

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_CHAIN_LENGTH = 128
MAX_VERIFIED_RECORDS = 512
MAX_LIST_LIMIT = 100
MAX_LIST_OFFSET = 100_000
ID_PATTERN = re.compile(r'protocol_[0-9a-f]{64}\Z')
BODY_FIELDS = {'title', 'note', 'parent_id', 'config', 'config_digest', 'changes',
               'semantics_version', 'sources', 'unresolved'}


def _core():
    # Keep the persistence module importable during schema/API initialization.
    from .. import research_protocol
    return research_protocol


def _identity(value, *, status=422):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ServiceError('Research protocol identity is invalid', status)
    return value


def _text(title, note):
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
        raise ServiceError('Research protocol title must contain 1..200 characters', 422)
    if not isinstance(note, str) or len(note) > 4000:
        raise ServiceError('Research protocol note must contain at most 4000 characters', 422)
    try:
        title.encode('utf-8')
        note.encode('utf-8')
    except UnicodeError as exc:
        raise ServiceError('Research protocol title and note must contain valid Unicode text', 422) from exc
    return title.strip(), note


def _config(value):
    try:
        return _core().validate_config(value)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ServiceError('Invalid research protocol configuration: ' + str(exc), 422) from exc


def _preset(mode):
    found = [item for item in _core().presets() if item['config']['mode'] == mode]
    if len(found) != 1:
        raise ServiceError('Server research protocol preset is unavailable or ambiguous', 409)
    return found[0]


def _changes(before, after):
    return [{'field': key, 'before': before[key], 'after': after[key]}
            for key in sorted(after) if before[key] != after[key]]


def _strings(value):
    valid = (isinstance(value, list) and len(value) <= 128
             and all(isinstance(item, str) and 0 < len(item) <= 4000 for item in value))
    if valid:
        try:
            for item in value:
                item.encode('utf-8')
        except UnicodeError:
            return False
    return valid


def _parse(payload):
    if not isinstance(payload, str) or len(payload.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError('Stored protocol exceeds its payload limit')

    def reject_constant(value):
        raise ValueError('Stored protocol contains a nonfinite number')

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Stored protocol contains duplicate JSON keys')
            result[key] = value
        return result

    return json.loads(payload, parse_constant=reject_constant, object_pairs_hook=unique_pairs)


class ResearchProtocols:
    def __init__(self, store):
        self.store = store

    def _record(self, row):
        """Check the saved snapshot; never refresh its frozen source statements."""
        try:
            body = _parse(row['payload'])
            if not isinstance(body, dict) or set(body) != BODY_FIELDS:
                raise ValueError('Stored protocol fields differ from the supported contract')
            if body['semantics_version'] != _core().SEMANTICS_VERSION:
                raise ValueError('Stored protocol semantics version is unsupported')
            if _text(body['title'], body['note']) != (body['title'], body['note']):
                raise ValueError('Stored protocol title is not normalized')
            normalized = _config(body['config'])
            if json_text(normalized) != json_text(body['config']):
                raise ValueError('Stored protocol configuration is not canonical')
            if body['config_digest'] != _core().config_digest(normalized):
                raise ValueError('Stored protocol configuration digest mismatch')
            if not _strings(body['sources']) or not _strings(body['unresolved']):
                raise ValueError('Stored protocol evidence is invalid')
            if not isinstance(body['changes'], list) or len(body['changes']) > len(normalized):
                raise ValueError('Stored protocol changes are invalid')
            if body['parent_id'] is not None:
                _identity(body['parent_id'])
            if body['parent_id'] != row['parent_id']:
                raise ValueError('Stored protocol parent identity mismatch')
            fingerprint = digest(body)
            if row['digest'] != fingerprint or row['id'] != 'protocol_' + fingerprint:
                raise ValueError('Stored protocol content digest mismatch')
            timestamp = datetime.fromisoformat(row['created_at'])
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError('Stored protocol timestamp needs a timezone')
            return {**body, 'id': row['id'], 'digest': fingerprint, 'created_at': row['created_at']}
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ServiceError('Stored research protocol failed integrity verification', 409) from exc

    def _load(self, connection, identity, cache):
        """Verify a bounded parent chain in one database snapshot, without recursion."""
        pending = []
        seen = set()
        current = identity
        while current is not None and current not in cache:
            if current in seen:
                raise ServiceError('Stored research protocol parent chain contains a cycle', 409)
            if len(pending) >= MAX_CHAIN_LENGTH or len(cache) + len(pending) >= MAX_VERIFIED_RECORDS:
                raise ServiceError('Research protocol ancestry exceeds the verification limit', 413)
            row = connection.execute('SELECT * FROM research_protocols WHERE id=?', (current,)).fetchone()
            if row is None:
                if pending:
                    raise ServiceError('Stored research protocol parent is missing', 409)
                raise ServiceError('Research protocol not found', 404)
            value = self._record(row)
            pending.append(value)
            seen.add(current)
            current = value['parent_id']
        for value in reversed(pending):
            parent_entry = cache.get(value['parent_id'])
            depth = parent_entry[1] + 1 if parent_entry else 1
            if depth > MAX_CHAIN_LENGTH:
                raise ServiceError('Research protocol ancestry exceeds the verification limit', 413)
            before = parent_entry[0]['config'] if parent_entry else _config(_preset(value['config']['mode'])['config'])
            if json_text(value['changes']) != json_text(_changes(before, value['config'])):
                raise ServiceError('Stored research protocol change list failed integrity verification', 409)
            cache[value['id']] = (value, depth)
        return cache[identity][0]

    def create(self, title, note, config, parent_id=None):
        title, note = _text(title, note)
        normalized = _config(config)
        if parent_id is not None:
            _identity(parent_id)
        preset = _preset(normalized['mode'])
        sources, unresolved = deepcopy(preset['sources']), deepcopy(preset['unresolved'])
        if not _strings(sources) or not _strings(unresolved):
            raise ServiceError('Server research protocol evidence is invalid', 409)
        with transaction(self.store.db_path) as connection:
            cache = {}
            parent = self._load(connection, parent_id, cache) if parent_id else None
            # Reserve one chain position for the new record as well.
            if parent is not None and len(cache) >= MAX_CHAIN_LENGTH:
                raise ServiceError('Research protocol ancestry exceeds the verification limit', 413)
            baseline = parent['config'] if parent else _config(preset['config'])
            body = {'title': title, 'note': note, 'parent_id': parent_id, 'config': normalized,
                    'config_digest': _core().config_digest(normalized), 'changes': _changes(baseline, normalized),
                    'semantics_version': _core().SEMANTICS_VERSION, 'sources': sources, 'unresolved': unresolved}
            payload = json_text(body)
            if len(payload.encode()) > MAX_PAYLOAD_BYTES:
                raise ServiceError('Research protocol exceeds 64 KiB', 413)
            fingerprint = digest(body)
            identity = 'protocol_' + fingerprint
            existing = connection.execute('SELECT id FROM research_protocols WHERE id=?', (identity,)).fetchone()
            if existing is not None:
                return deepcopy(self._load(connection, identity, cache))
            created_at = now()
            connection.execute('INSERT INTO research_protocols(id,payload,digest,created_at,parent_id) VALUES (?,?,?,?,?)',
                               (identity, payload, fingerprint, created_at, parent_id))
            return {**body, 'id': identity, 'digest': fingerprint, 'created_at': created_at}

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            return deepcopy(self._load(connection, identity, {}))

    def list(self, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ServiceError('Research protocol list limit must be an integer between 1 and 100', 422)
        if type(offset) is not int or not 0 <= offset <= MAX_LIST_OFFSET:
            raise ServiceError('Research protocol list offset must be an integer between 0 and 100000', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM research_protocols').fetchone()[0]
            identities = connection.execute('SELECT id FROM research_protocols ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                                            (limit, offset)).fetchall()
            cache = {}
            items = [deepcopy(self._load(connection, row['id'], cache)) for row in identities]
            return {'items': items, 'total': total, 'limit': limit, 'offset': offset}
