"""Immutable exact author research references. HTTP never reads raw MAT files."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import stat

from .db import connect, transaction
from .service import ServiceError, now
from .author_panels import AuthorPanels, _metadata_digest, _verify_metadata, _timestamp
from .author_studies import AuthorStudies
from .research_protocols import ResearchProtocols
from .research_bindings_schema import (BindingPrepareRequest, BindingPreviewRequest, BindingCreate,
                                       BindingPreview, ResearchBinding)
from .. import research_binding
from ..evidence import sha256, verify_paper
from ..storage import digest, json_text

SCHEMA = """
CREATE TABLE research_bindings(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL);
CREATE INDEX research_bindings_created ON research_bindings(created_at,id);
CREATE TABLE research_binding_receipts(idempotency_key TEXT PRIMARY KEY,request_digest TEXT NOT NULL,binding_id TEXT NOT NULL REFERENCES research_bindings(id),binding_digest TEXT NOT NULL,binding_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL);
"""
MAX_BYTES = 2 * 1024 * 1024
MAX_LEDGER_BYTES = 16 * 1024 * 1024
BODY_FIELDS = {'request', 'preview_digest', 'context'}
RECEIPT_FIELDS = {'idempotency_key', 'request_digest', 'binding_id', 'binding_digest', 'binding_metadata_digest', 'created_at'}


def _parse(text):
    if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
        raise ValueError('Binding JSON exceeds its byte bound')
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('Duplicate binding field')
            out[key] = value
        return out
    def invalid(_):
        raise ValueError('Nonfinite binding number')
    result = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    if json_text(result) != text:
        raise ValueError('Noncanonical binding JSON')
    return result


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'research_binding_[0-9a-f]{64}', value):
        raise ServiceError('Invalid research binding identity', 422)


class ResearchBindings:
    def __init__(self, store):
        self.store = store

    def _resources(self, paper_id, protocol_id, study_id):
        with closing(connect(self.store.db_path)) as connection:
            size = connection.execute('SELECT typeof(document) AS type,length(CAST(document AS BLOB)) AS bytes FROM papers WHERE id=?', (paper_id,)).fetchone()
            if size is None:
                raise ServiceError('Selected paper not found', 404)
            if size['type'] != 'text' or size['bytes'] > MAX_BYTES:
                raise ServiceError('Selected fixed paper extraction exceeds its bounded identity', 409)
            paper = self.store._one(connection, 'papers', paper_id)
        paper['document'] = _parse(paper['document'])
        path = Path(paper['pdf_path']).absolute()
        info = path.lstat()
        if (path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not 0 < info.st_size <= 16 * 1024 * 1024):
            raise ServiceError('Selected paper file no longer has a bounded independent identity', 409)
        if paper['sha256'] != sha256(paper['pdf_path']):
            raise ServiceError('Selected paper bytes changed', 409)
        verify_paper(paper['document'], paper['pdf_path'])
        protocol = ResearchProtocols(self.store).get(protocol_id)
        study = AuthorStudies(self.store).get(study_id)
        panels = [AuthorPanels(self.store).get(identity) for identity in study['author_panel_ids']]
        return paper, protocol, study, panels

    def prepare(self, paper_id, protocol_id, study_id, source_scope, title, note=''):
        """Derive exact references from existing choices, then return a preview."""
        try:
            selected = BindingPrepareRequest.model_validate(dict(paper_id=paper_id, protocol_id=protocol_id,
                study_id=study_id, source_scope=source_scope, title=title, note=note)).model_dump()
            paper, protocol, study, panels = self._resources(paper_id, protocol_id, study_id)
            request = {**selected, 'paper_digest': paper['sha256'], 'protocol_digest': protocol['digest'],
                'study_digest': study['digest'], 'source_filename': study['scan']['source']['filename'],
                'source_sha256': study['scan']['source']['sha256'], 'plan_digest': digest(study['scan']['plan']),
                'scan_digest': digest(study['scan']), 'result_digest': digest(study['result']),
                'panels': [{'id': p['id'], 'digest': p['digest']} for p in panels]}
            return self.preview(**request)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('Research binding preparation cannot verify the selected references', 422) from exc

    def preview(self, **request):
        with closing(connect(self.store.db_path)) as connection:
            self._ledger(connection)
        return self._preview(**request)

    def _preview(self, **request):
        try:
            request = BindingPreviewRequest.model_validate(request).model_dump()
            resources = self._resources(request['paper_id'], request['protocol_id'], request['study_id'])
            context = research_binding.project(request, *resources)
            body = {'request': request, 'context': context}
            preview = {**body, 'preview_digest': digest(body)}
            if len(json_text(preview).encode()) > MAX_BYTES:
                raise ValueError('Bounded binding projection exceeded')
            return BindingPreview.model_validate(preview).model_dump()
        except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('Selected paper/protocol/source/scan/panels do not form a compatible exact research binding', 422) from exc

    def _record(self, row):
        try:
            body = _parse(row['payload'])
            if not isinstance(body, dict) or set(body) != BODY_FIELDS:
                raise ValueError('Binding fields changed')
            fingerprint = digest(body)
            if row['id'] != 'research_binding_' + fingerprint or row['digest'] != fingerprint:
                raise ValueError('Binding identity changed')
            _verify_metadata(row)
            current = self._preview(**body['request'])
            if current['context'] != body['context'] or current['preview_digest'] != body['preview_digest']:
                raise ValueError('Original binding references changed')
            value = ResearchBinding.model_validate({**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at']}).model_dump()
            if json_text({k: value[k] for k in BODY_FIELDS}) != row['payload']:
                raise ValueError('Binding types changed through normalization')
            return value
        except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
            raise ServiceError('Research binding failed source or record integrity verification', 409) from exc

    def _ledger(self, connection):
        """Every durable body needs a valid exact-request receipt; never rebuild lost anchors."""
        sizes = connection.execute("SELECT typeof(payload) AS type,length(CAST(payload AS BLOB)) AS bytes, "
            "(typeof(id)='text' AND typeof(digest)='text' AND typeof(created_at)='text' AND typeof(metadata_digest)='text') AS text_metadata, "
            "length(CAST(id AS BLOB))+length(CAST(digest AS BLOB))+length(CAST(created_at AS BLOB))+length(CAST(metadata_digest AS BLOB)) AS metadata_bytes "
            "FROM research_bindings LIMIT 1001").fetchall()
        receipt_columns = sorted(RECEIPT_FIELDS | {'receipt_digest'})
        types = ' AND '.join("typeof(" + name + ")='text'" for name in receipt_columns)
        length = '+'.join('length(CAST(' + name + ' AS BLOB))' for name in receipt_columns)
        receipt_sizes = connection.execute('SELECT (' + types + ') AS text_types,(' + length + ') AS bytes FROM research_binding_receipts LIMIT 10001').fetchall()
        if any(row['type'] != 'text' or not row['text_metadata'] for row in sizes) or any(not row['text_types'] for row in receipt_sizes):
            raise ServiceError('Binding ledger storage types failed verification', 409)
        if (len(sizes) > 1000 or len(receipt_sizes) > 10000 or any(row['bytes'] > MAX_BYTES for row in sizes)
                or any(row['metadata_bytes'] > 512 for row in sizes) or any(row['bytes'] > 2048 for row in receipt_sizes)
                or sum(row['bytes'] for row in sizes) + sum(row['bytes'] for row in receipt_sizes) > MAX_LEDGER_BYTES):
            raise ServiceError('Binding ledger exceeds its bounded record/receipt bytes', 413)
        rows = connection.execute('SELECT * FROM research_bindings ORDER BY id LIMIT 1001').fetchall()
        receipts = connection.execute('SELECT * FROM research_binding_receipts ORDER BY idempotency_key LIMIT 10001').fetchall()
        if len(rows) > 1000 or len(receipts) > 10000:
            raise ServiceError('Binding ledger exceeds its verification bound', 413)
        values = {row['id']: self._record(row) for row in rows}
        metadata = {row['id']: row['metadata_digest'] for row in rows}
        covered = set()
        try:
            for row in receipts:
                item = dict(row)
                if set(item) != RECEIPT_FIELDS | {'receipt_digest'}:
                    raise ValueError('Receipt fields changed')
                _timestamp(item['created_at'])
                value = values.get(item['binding_id'])
                if (not isinstance(item['idempotency_key'], str) or not item['idempotency_key'].strip()
                        or len(item['idempotency_key']) > 128 or value is None
                        or digest({key: item[key] for key in RECEIPT_FIELDS}) != item['receipt_digest']
                        or item['binding_digest'] != value['digest'] or item['binding_metadata_digest'] != metadata[value['id']]
                        or datetime.fromisoformat(item['created_at']) < datetime.fromisoformat(value['created_at'])
                        or datetime.fromisoformat(item['created_at']) > datetime.now(timezone.utc)
                        or item['request_digest'] != digest({'request': value['request'], 'preview_digest': value['preview_digest']})):
                    raise ValueError('Receipt no longer anchors its exact original request')
                covered.add(item['binding_id'])
            if covered != set(values):
                raise ValueError('An original research binding receipt is missing')
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError('Research binding complete receipt ledger failed verification', 409) from exc
        return values

    def _receipt(self, connection, key, fingerprint):
        receipt = connection.execute('SELECT * FROM research_binding_receipts WHERE idempotency_key=?', (key,)).fetchone()
        if receipt is None:
            return None
        try:
            if set(dict(receipt)) != RECEIPT_FIELDS | {'receipt_digest'} or digest({k: receipt[k] for k in RECEIPT_FIELDS}) != receipt['receipt_digest']:
                raise ValueError('Receipt anchor differs')
            if receipt['request_digest'] != fingerprint:
                raise ServiceError('Idempotency key belongs to another research binding request', 409)
            row = connection.execute('SELECT * FROM research_bindings WHERE id=?', (receipt['binding_id'],)).fetchone()
            if (row is None or row['digest'] != receipt['binding_digest'] or row['metadata_digest'] != receipt['binding_metadata_digest']):
                raise ValueError('Receipt binding changed')
            return self._record(row)
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError('Research binding receipt failed verification', 409) from exc

    def create(self, **request):
        try:
            modeled = BindingCreate.model_validate(request).model_dump()
            key = modeled.pop('idempotency_key')
            if not key.strip():
                raise ValueError('Blank key')
            expected_preview = modeled.pop('preview_digest')
            fingerprint = digest({'request': modeled, 'preview_digest': expected_preview})
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError('Invalid closed research binding request', 422) from exc
        with closing(connect(self.store.db_path)) as connection:
            self._ledger(connection)
            replay = self._receipt(connection, key, fingerprint)
            if replay:
                return replay
        preview = self.preview(**modeled)
        if preview['preview_digest'] != expected_preview:
            raise ServiceError('Research binding preview changed; inspect the exact references again', 409)
        body = {k: preview[k] for k in BODY_FIELDS}
        identity, fingerprint_body, created = 'research_binding_' + digest(body), digest(body), now()
        row = {'id': identity, 'payload': json_text(body), 'digest': fingerprint_body, 'created_at': created,
               'metadata_digest': _metadata_digest(identity, fingerprint_body, created)}
        with transaction(self.store.db_path) as connection:
            self._ledger(connection)
            replay = self._receipt(connection, key, fingerprint)
            if replay:
                return replay
            # Store writers are excluded; fresh readers below see the same
            # original resources. PDF files get another actual check as well.
            if self.preview(**modeled) != preview:
                raise ServiceError('Binding sources changed during preparation', 409)
            existing = connection.execute('SELECT * FROM research_bindings WHERE id=?', (identity,)).fetchone()
            if existing:
                value = self._record(existing)
                row = dict(existing)
            else:
                connection.execute('INSERT INTO research_bindings VALUES (?,?,?,?,?)', tuple(row.values()))
                value = ResearchBinding.model_validate({**body, 'id': identity, 'digest': fingerprint_body, 'created_at': created}).model_dump()
            receipt = {'idempotency_key': key, 'request_digest': fingerprint, 'binding_id': identity,
                'binding_digest': row['digest'], 'binding_metadata_digest': row['metadata_digest'], 'created_at': now()}
            connection.execute('INSERT INTO research_binding_receipts VALUES (?,?,?,?,?,?,?)',
                tuple(receipt.values()) + (digest(receipt),))
            # Re-read actual selected PDF/resources after publishing both rows
            # into this transaction, before the only durable commit.
            self._ledger(connection)
            return value

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            values = self._ledger(connection)
            if identity not in values:
                raise ServiceError('Research binding not found', 404)
            return values[identity]

    def list(self, limit=20, offset=0, study_id=None):
        if (type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000
                or study_id is not None and (not isinstance(study_id, str) or not re.fullmatch(r'author_study_[0-9a-f]{64}', study_id))):
            raise ServiceError('Invalid binding pagination/study identity', 422)
        with closing(connect(self.store.db_path)) as connection:
            self._ledger(connection)
            # Verify each record before filtering; a broken unrelated row is
            # not silently hidden by the requested source filter.
            rows = connection.execute('SELECT * FROM research_bindings ORDER BY created_at DESC,id')
            items, total, seen = [], 0, 0
            for row in rows:
                seen += 1
                if seen > 1000:
                    raise ServiceError('Binding list exceeds its verification bound', 413)
                value = self._record(row)
                if study_id and value['request']['study_id'] != study_id:
                    continue
                total += 1
                if offset <= total - 1 < offset + limit:
                    request, context = value['request'], value['context']
                    items.append({k: value[k] for k in ('id', 'digest', 'created_at')} | {
                        'title': request['title'], 'source_scope': request['source_scope'], 'paper_id': request['paper_id'],
                        'protocol_id': request['protocol_id'], 'study_id': request['study_id'], 'source_filename': request['source_filename'],
                        'status': context['status'], 'blockers': context['blockers'], 'raw_source_reverified': False, 'execution_ready': False})
            return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def export(self, identity):
        value = self.get(identity)
        return {'schema_version': 1, 'kind': 'research_binding_export', 'binding': value, 'binding_digest': value['digest'],
                'scope': 'Exact source-linked blocked preparation; no raw MAT authentication or portfolio authority.'}

    def markdown(self, identity):
        return '# Source-linked author research preparation\n\nNo portfolio execution or human approval.\n\n```json\n' + json_text(self.export(identity)) + '\n```\n'
