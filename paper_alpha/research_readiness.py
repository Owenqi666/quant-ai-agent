"""Offline declaration/integrity gates; never parse market values or run research.

A complete contract is not a data-quality, authorization, human-identity or
scientific-independence approval. Existing synthetic-only execution is untouched.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

VERSION = 'research-readiness-v1'
ROOT = Path(__file__).resolve().parents[1]
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 32
DEV_MANIFESTS = ('evaluation_suites/v04/manifest.json', 'evaluation_suites/v05/manifest.json',
                 'evaluation_suites/v06/manifest.json')
SHA = re.compile(r'^[0-9a-f]{64}$')
UNKNOWN = {'', 'unknown', 'unspecified', 'pending', 'tbd', 'todo', '未提供', '未知', '待填写', '待确认', 'n/a', 'na', 'none', 'null', 'not provided', 'not available'}


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def freeze_digest(manifest):
    """Bind every declaration/artifact reference except the freeze envelope itself."""
    return sha256(canonical({key: value for key, value in manifest.items() if key != 'freeze'}))


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def reject(value):
        raise ValueError('nonfinite_json_number')
    def finite(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('nonfinite_json_number')
        return parsed
    return json.loads(data, object_pairs_hook=pairs, parse_constant=reject, parse_float=finite)


class UnsafeInput(ValueError):
    pass


class InputRoot:
    """Descriptor-relative regular-file reads with bounded bytes and no symlinks."""
    def __init__(self, root):
        self.path = Path(root).absolute()
        self.fd = None
        self.total = 0

    def __enter__(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open('/', flags)
        try:
            for component in self.path.parts[1:]:
                if component in {'.', '..'}:
                    raise UnsafeInput('unsafe_root')
                next_fd = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            self.fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def __exit__(self, *args):
        os.close(self.fd)

    @staticmethod
    def parts(path):
        if not isinstance(path, str) or not path or len(path) > 512 or '\\' in path or '\x00' in path:
            raise UnsafeInput('unsafe_path')
        parts = path.split('/')
        if len(parts) > 16 or any(part in {'', '.', '..'} or ':' in part for part in parts):
            raise UnsafeInput('unsafe_path')
        return parts

    @contextmanager
    def open_file(self, path):
        parts = self.parts(path)
        directory = os.dup(self.fd)
        file = None
        try:
            for component in parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                os.close(directory)
                directory = next_fd
            file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            info = os.fstat(file)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeInput('not_independent_regular_file')
            yield file, info
        finally:
            if file is not None:
                os.close(file)
            os.close(directory)

    def read(self, path, *, limit=MAX_FILE_BYTES, retain=False):
        with self.open_file(path) as (fd, before):
            if not 0 < before.st_size <= limit or self.total + before.st_size > MAX_TOTAL_BYTES:
                raise UnsafeInput('input_size_limit')
            total = 0
            hasher = hashlib.sha256()
            chunks = []
            while True:
                chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit or total > before.st_size:
                    raise UnsafeInput('input_changed_during_read')
                hasher.update(chunk)
                if retain:
                    chunks.append(chunk)
            after = os.fstat(fd)
            fingerprint = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
            if total != before.st_size or fingerprint(before) != fingerprint(after):
                raise UnsafeInput('input_changed_during_read')
            # Reopen from the root to detect replaced names/ancestor directories.
            with self.open_file(path) as (_, current):
                if fingerprint(current) != fingerprint(before):
                    raise UnsafeInput('input_changed_during_read')
        self.total += total
        return {'path': path, 'sha256': hasher.hexdigest(), 'size': total}, b''.join(chunks) if retain else None


def paper_identity(value):
    value = value.strip().lower()
    match = re.fullmatch(r'(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?', value)
    return 'arxiv:' + match[1] if match else value


def development_registry():
    """Current bundled development identities; user manifests cannot replace them."""
    sources, hashes, ids = {}, set(), set()
    with InputRoot(ROOT) as root:
        for path in DEV_MANIFESTS:
            receipt, raw = root.read(path, limit=MAX_MANIFEST_BYTES, retain=True)
            sources[path] = receipt['sha256']
            manifest = strict_json(raw)
            for task in manifest['tasks']:
                if task.get('partition') != 'development':
                    continue
                ids.add(task['id'].casefold())
                ids.update(value.casefold() for value in task.get('family_ids', []))
                hashes.update(task.get('input_sha256', {}).values())
        path = 'examples/alpha101/paper.json'
        receipt, raw = root.read(path, limit=MAX_MANIFEST_BYTES, retain=True)
        sources[path] = receipt['sha256']
        paper = strict_json(raw)
        identities = [paper_identity(paper['id'])]
        titles = [re.sub(r'\W+', '', paper['title'].casefold())]
        hashes.add(paper['document_sha256'])
    payload = {'schema_version': 1, 'sources': sources, 'paper_ids': identities,
               'paper_titles': titles, 'input_ids': sorted(ids), 'input_sha256': sorted(hashes)}
    return {**payload, 'sha256': sha256(canonical(payload))}


class Inspector:
    def __init__(self, root):
        self.root = root
        self.reasons = []
        self.files = []
        self.paths = set()
        self.shared_development_context = []

    def fail(self, code, location, message):
        self.reasons.append({'code': code, 'location': location, 'message': message})

    def obj(self, value, fields, location):
        if not isinstance(value, dict):
            self.fail('object_required', location, 'A structured object is required.')
            return {}
        if set(value) - set(fields):
            self.fail('unknown_fields', location, 'Unknown fields are not accepted.')
        for key in fields:
            if key not in value:
                self.fail('missing_field', location + '.' + key, 'Required field is absent.')
        return value

    def text(self, value, location):
        if not isinstance(value, str) or len(value) > 4000 or value.strip().casefold() in UNKNOWN:
            self.fail('declaration_missing', location, 'Provide an explicit non-placeholder declaration.')
            return False
        return True

    def equal(self, value, expected, location):
        if type(value) is not type(expected) or value != expected:
            self.fail('prerequisite_unmet', location, 'This prerequisite must be explicitly declared before contract completion.')

    def items(self, value, location, *, minimum=1, maximum=MAX_FILES):
        if not isinstance(value, list) or not minimum <= len(value) <= maximum:
            self.fail('list_bounds', location, 'List length is outside the documented bounds.')
            return []
        return value

    def artifact(self, value, location, *, purpose=None):
        value = self.obj(value, ('path', 'sha256'), location)
        path, expected = value.get('path'), value.get('sha256')
        if not isinstance(expected, str) or not SHA.fullmatch(expected):
            self.fail('sha256_required', location + '.sha256', 'A lowercase SHA-256 digest is required.')
            return None
        if len(self.files) >= MAX_FILES:
            self.fail('artifact_limit', location, 'At most 32 artifacts may be checked.')
            return None
        try:
            InputRoot.parts(path)
            if path in self.paths:
                self.fail('duplicate_artifact_path', location, 'Each artifact must have a distinct relative path.')
                return None
            self.paths.add(path)
            receipt, _ = self.root.read(path)
        except (OSError, ValueError) as exc:
            code = str(exc) if isinstance(exc, UnsafeInput) else 'artifact_unavailable_or_unsafe'
            self.fail(code, location, 'Artifact must be a bounded independent regular file under the input root.')
            return None
        self.files.append({**receipt, 'role': location, **({'purpose': purpose} if purpose else {})})
        if receipt['sha256'] != expected:
            self.fail('artifact_digest_mismatch', location, 'Current artifact bytes differ from the declared frozen digest.')
        return receipt

    def real_data(self, manifest):
        fields = ('schema_version', 'kind', 'id', 'data_kind', 'declaration_source', 'source',
                  'authorization', 'snapshots', 'field_mapping', 'semantics')
        manifest = self.obj(manifest, fields, 'manifest')
        self.text(manifest.get('id'), 'id')
        kind = manifest.get('data_kind')
        if kind not in ('real_market', 'controlled_fixture'):
            self.fail('data_kind_unknown', 'data_kind', 'Declare real_market or controlled_fixture; no data is validated by this declaration.')
        source_kind = manifest.get('declaration_source')
        if source_kind not in ('human', 'automation'):
            self.fail('source_unknown', 'declaration_source', 'The source of this declaration is required.')
        if kind == 'real_market':
            self.equal(source_kind, 'human', 'declaration_source')
        source = self.obj(manifest.get('source'), ('provider', 'dataset', 'version', 'reference'), 'source')
        for name in ('provider', 'dataset', 'version', 'reference'):
            self.text(source.get(name), 'source.' + name)
        authorization = self.obj(manifest.get('authorization'), ('status', 'basis', 'permitted_use', 'evidence'), 'authorization')
        self.equal(authorization.get('status'), 'declared_authorized', 'authorization.status')
        self.text(authorization.get('basis'), 'authorization.basis')
        self.text(authorization.get('permitted_use'), 'authorization.permitted_use')
        self.artifact(authorization.get('evidence'), 'authorization.evidence')
        snapshots = self.items(manifest.get('snapshots'), 'snapshots', maximum=16)
        ids, roles = set(), {}
        for index, item in enumerate(snapshots):
            location = f'snapshots[{index}]'
            item = self.obj(item, ('id', 'role', 'artifact'), location)
            identity = item.get('id')
            if self.text(identity, location + '.id'):
                if identity in ids:
                    self.fail('duplicate_snapshot_id', location, 'Snapshot IDs must be unique.')
                ids.add(identity)
            role = item.get('role')
            if role not in ('market_data', 'calendar', 'universe', 'corporate_actions'):
                self.fail('snapshot_role_unknown', location + '.role', 'Use market_data, calendar, universe or corporate_actions.')
            elif isinstance(identity, str):
                roles[identity] = role
            self.artifact(item.get('artifact'), location + '.artifact')
        for required in ('market_data', 'calendar', 'universe'):
            if required not in roles.values():
                self.fail('snapshot_role_missing', 'snapshots', 'A ' + required + ' snapshot is required.')
        mappings = self.items(manifest.get('field_mapping'), 'field_mapping', maximum=128)
        targets = set()
        for index, item in enumerate(mappings):
            location = f'field_mapping[{index}]'
            item = self.obj(item, ('target', 'source', 'unit', 'availability_time_rule', 'revision_policy', 'snapshot_id'), location)
            for name in ('target', 'source', 'unit', 'availability_time_rule', 'revision_policy'):
                self.text(item.get(name), location + '.' + name)
            snapshot_id = item.get('snapshot_id')
            if not isinstance(snapshot_id, str) or roles.get(snapshot_id) != 'market_data':
                self.fail('field_snapshot_missing', location + '.snapshot_id', 'Map each field to a market_data snapshot.')
            target = item.get('target')
            if isinstance(target, str):
                if target in targets:
                    self.fail('duplicate_target_field', location, 'Each target field needs one explicit mapping.')
                targets.add(target)
        semantics = self.obj(manifest.get('semantics'),
            ('timezone', 'timestamp_key', 'asset_key', 'calendar_snapshot_id', 'calendar_rule',
             'adjustment_rule', 'adjustment_as_of_rule', 'universe_snapshot_id', 'historical_universe_rule',
             'missing_value_rule', 'suspension_rule', 'delisting_rule'), 'semantics')
        for name in ('timezone', 'timestamp_key', 'asset_key', 'calendar_rule', 'adjustment_rule', 'adjustment_as_of_rule',
                     'historical_universe_rule', 'missing_value_rule', 'suspension_rule', 'delisting_rule'):
            self.text(semantics.get(name), 'semantics.' + name)
        for name in ('calendar_snapshot_id', 'universe_snapshot_id'):
            reference = semantics.get(name)
            if not isinstance(reference, str) or roles.get(reference) != name.removesuffix('_snapshot_id'):
                self.fail('snapshot_reference_missing', 'semantics.' + name, 'Reference a declared calendar/universe snapshot.')
        return 'ready_for_adapter_review' if kind == 'real_market' else 'contract_complete'

    def heldout(self, manifest, registry, as_of):
        manifest = self.obj(manifest, ('schema_version', 'kind', 'id', 'material_kind', 'paper', 'task',
                                      'manual_expected', 'additional_inputs', 'development_exposure', 'freeze'), 'manifest')
        self.text(manifest.get('id'), 'id')
        if manifest.get('material_kind') not in ('research_material', 'controlled_fixture'):
            self.fail('material_kind_unknown', 'material_kind', 'Declare research_material or controlled_fixture.')
        paper = self.obj(manifest.get('paper'), ('id', 'title', 'source_reference', 'artifact'), 'paper')
        for name in ('id', 'title', 'source_reference'):
            self.text(paper.get(name), 'paper.' + name)
        if isinstance(paper.get('id'), str) and paper_identity(paper['id']) in registry['paper_ids']:
            self.fail('development_paper_overlap', 'paper.id', 'This paper identity already belongs to the development material.')
        if isinstance(paper.get('title'), str) and re.sub(r'\W+', '', paper['title'].casefold()) in registry['paper_titles']:
            self.fail('development_paper_overlap', 'paper.title', 'This title identifies already-used development material.')
        self.artifact(paper.get('artifact'), 'paper.artifact', purpose='heldout_material')
        task = self.obj(manifest.get('task'), ('id', 'artifact'), 'task')
        self.text(task.get('id'), 'task.id')
        if isinstance(task.get('id'), str) and task['id'].casefold() in registry['input_ids']:
            self.fail('development_input_id_overlap', 'task.id', 'Task ID already occurs in the bundled development inventory.')
        self.artifact(task.get('artifact'), 'task.artifact', purpose='heldout_material')
        expected = self.obj(manifest.get('manual_expected'), ('source', 'declarant', 'label_status', 'artifact'), 'manual_expected')
        self.equal(expected.get('source'), 'human', 'manual_expected.source')
        self.equal(expected.get('label_status'), 'completed', 'manual_expected.label_status')
        self.text(expected.get('declarant'), 'manual_expected.declarant')
        self.artifact(expected.get('artifact'), 'manual_expected.artifact', purpose='heldout_material')
        input_ids = {task.get('id')} if isinstance(task.get('id'), str) else set()
        for index, item in enumerate(self.items(manifest.get('additional_inputs'), 'additional_inputs', minimum=0, maximum=16)):
            location = f'additional_inputs[{index}]'
            item = self.obj(item, ('id', 'role', 'artifact'), location)
            identity = item.get('id')
            purpose = item.get('role')
            if purpose not in ('heldout_material', 'shared_context'):
                self.fail('input_role_unknown', location + '.role', 'Declare heldout_material or shared_context explicitly.')
            known_id = isinstance(identity, str) and identity.casefold() in registry['input_ids']
            if self.text(identity, location + '.id'):
                if known_id and purpose != 'shared_context':
                    self.fail('development_input_id_overlap', location + '.id', 'Input ID belongs to known development material.')
                if identity in input_ids:
                    self.fail('duplicate_input_id', location + '.id', 'Input IDs must be unique.')
                input_ids.add(identity)
            receipt = self.artifact(item.get('artifact'), location + '.artifact', purpose=purpose)
            known_digest = receipt is not None and receipt['sha256'] in registry['input_sha256']
            if purpose == 'shared_context' and (known_id or known_digest):
                self.shared_development_context.append({
                    'location': location, 'id': identity,
                    'path': receipt['path'] if receipt else None,
                    'sha256': receipt['sha256'] if receipt else None,
                    'matched_by': (['id'] if known_id else []) + (['sha256'] if known_digest else [])})
        for receipt in self.files:
            if receipt.get('purpose') != 'shared_context' and receipt['sha256'] in registry['input_sha256']:
                self.fail('development_input_digest_overlap', receipt['role'], 'Artifact bytes already occur in the development inventory; renaming does not create a held-out sample.')
        exposure = self.obj(manifest.get('development_exposure'),
                            ('source', 'declarant', 'paper_used', 'task_used', 'expected_used'), 'development_exposure')
        self.equal(exposure.get('source'), 'human', 'development_exposure.source')
        self.text(exposure.get('declarant'), 'development_exposure.declarant')
        for name in ('paper_used', 'task_used', 'expected_used'):
            self.equal(exposure.get(name), False, 'development_exposure.' + name)
        frozen = self.obj(manifest.get('freeze'), ('status', 'frozen_at', 'input_digest', 'execution_state', 'execution_started_at'), 'freeze')
        self.equal(frozen.get('status'), 'frozen', 'freeze.status')
        self.equal(frozen.get('execution_state'), 'not_started', 'freeze.execution_state')
        self.equal(frozen.get('execution_started_at'), None, 'freeze.execution_started_at')
        if frozen.get('input_digest') != freeze_digest(manifest):
            self.fail('freeze_digest_mismatch', 'freeze.input_digest', 'Freeze must bind all manifest declarations and artifact references.')
        def timestamp(value):
            if not isinstance(value, str):
                raise ValueError
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            return parsed
        try:
            frozen_at, reference = timestamp(frozen.get('frozen_at')), timestamp(as_of)
            if frozen_at > reference:
                self.fail('freeze_in_future', 'freeze.frozen_at', 'Freeze timestamp must not follow the explicit check reference time.')
        except (ValueError, TypeError):
            self.fail('freeze_time_unconfirmed', 'freeze.frozen_at', 'Provide timezone-aware frozen_at and an explicit --as-of reference time.')
        return 'contract_complete'


def check_readiness(root, manifest_path, *, as_of=None):
    report = {'schema_version': 1, 'checker_version': VERSION,
              'checker_sha256': sha256(Path(__file__).read_bytes()), 'status': 'blocked',
              'kind': None, 'material_scope': None, 'manifest_sha256': None, 'contract_sha256': None, 'as_of': as_of,
              'files': [], 'reasons': [], 'development_registry': None, 'shared_development_context': [],
              'execution': {'enabled': False, 'synthetic_only_lock': True, 'final_test_unlocked': False},
              'claims': {'market_values_validated': False, 'authorization_verified': False,
                         'human_identity_verified': False, 'scientific_independence_approved': False,
                         'market_holdout_validated': False, 'data_leakage_absence_verified': False},
              'limits': {'max_manifest_bytes': MAX_MANIFEST_BYTES, 'max_file_bytes': MAX_FILE_BYTES,
                         'max_total_bytes': MAX_TOTAL_BYTES, 'max_artifacts': MAX_FILES},
              'limitations': ['Declarations and matching file digests are preparation for review, not permission or scientific approval.',
                              'Market bytes are hashed only; no values, corporate actions, calendars or timing semantics are validated.',
                              'Known development overlap detection cannot establish absence of all prior exposure or semantically equivalent material.',
                              'Shared context may reuse development data; research-material holdout does not establish a market-time holdout or absence of leakage.',
                              'Freeze timestamps and human sources are declarations; this is not a trusted timestamp or identity service.',
                              'No research is executed, no final test is opened, and no network is used.']}
    inspector = None
    try:
        with InputRoot(root) as reader:
            inspector = Inspector(reader)
            receipt, raw = reader.read(manifest_path, limit=MAX_MANIFEST_BYTES, retain=True)
            report['manifest_sha256'] = receipt['sha256']
            manifest = strict_json(raw)
            if not isinstance(manifest, dict):
                raise ValueError('manifest_object_required')
            report['kind'] = manifest.get('kind') if manifest.get('kind') in ('real_data', 'heldout_study') else None
            scope = manifest.get('data_kind', manifest.get('material_kind'))
            report['material_scope'] = scope if scope in ('controlled_fixture', 'real_market', 'research_material') else None
            report['contract_sha256'] = freeze_digest(manifest)
            inspector.equal(manifest.get('schema_version'), 1, 'schema_version')
            if manifest.get('kind') == 'real_data':
                result = inspector.real_data(manifest)
            elif manifest.get('kind') == 'heldout_study':
                registry = development_registry()
                report['development_registry'] = registry
                result = inspector.heldout(manifest, registry, as_of)
            else:
                inspector.fail('unsupported_kind', 'kind', 'Use real_data or heldout_study.')
                result = 'blocked'
            if not inspector.reasons:
                report['status'] = result
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        report['reasons'].append({'code': 'manifest_unavailable_or_invalid', 'location': 'manifest',
                                  'message': 'Manifest/registry must be bounded strict JSON in safe regular files; no readiness is established.'})
    if inspector:
        report['files'] = inspector.files
        report['shared_development_context'] = inspector.shared_development_context
        report['reasons'].extend(inspector.reasons)
    report['report_sha256'] = sha256(canonical(report))
    return report
