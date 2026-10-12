"""Bounded immutable business capture, copied before private state cleanup.

This is a projection of the original supervisor journal, never a new authority
ledger. Historical validation reads only this fixed directory and does not
follow its saved temporary paths or native authentication metadata.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat

from ..storage import digest as journal_digest
from .codex_cli_provider import _atomic_bytes
from .provider_app_server_capture import (
    AppServerCaptureError, CAPTURE_VERSION, CAPTURE_VERSION_V2, CAPTURE_VERSION_V3,
    capture_version_for_transport,
    project_business_capture,
)
from .provider_contract import canonical_json, digest, strict_json
from .provider_supervision_journal import MAX_STAGES, validate_stop

EVIDENCE_VERSION = 'business_app_server_persistent_evidence_v1'
EVIDENCE_VERSION_V2 = 'business_app_server_persistent_evidence_v2'
EVIDENCE_VERSION_V3 = 'business_app_server_persistent_evidence_v3'
_JSON_LIMIT = 65536
_LIMITS = {'plan.json': 131072, 'invocation.json': 524288, 'identity.json': 524288,
           'spec.json': 524288, 'stdin.bin': 131072, 'ready.json': 4096,
           'launched.json': 4096, 'stop.json': _JSON_LIMIT,
           'rpc-result.json': _JSON_LIMIT, 'stdout.bin': 2097152,
           'stderr.bin': 2097152, 'response.raw.json': 65536,
           'journal.binding.json': _JSON_LIMIT, 'journal.prepared.json': _JSON_LIMIT,
           'journal.started.json': _JSON_LIMIT, 'journal.finished.json': _JSON_LIMIT}
_OPTIONAL = {'launched.json', 'response.raw.json'}
_BINDING_KEYS = {'schema_version', 'workspace_identity', 'proposal_id', 'attempt_id',
                 'fencing_token', 'prepared_digest', 'config_digest',
                 'attempt_started_digest', 'host_boot'}


class AppServerEvidenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def evidence_version_for_transport(transport_version):
    """A fixed transport selects its projection, never caller/model preference."""
    try:
        version = capture_version_for_transport(transport_version)
    except AppServerCaptureError as exc:
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_ENVELOPE_INVALID') from exc
    return {CAPTURE_VERSION: EVIDENCE_VERSION, CAPTURE_VERSION_V2: EVIDENCE_VERSION_V2,
            CAPTURE_VERSION_V3: EVIDENCE_VERSION_V3}[version]


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _path(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(node.is_symlink() for node in (path, *path.parents)):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_PATH_INVALID')
    return path


def _read(path, limit):
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise AppServerEvidenceError('APP_SERVER_EVIDENCE_FILE_INVALID')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) != before.st_size or len(raw) > limit or
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_FILE_DRIFT')
    return raw


def _json(raw, limit=_JSON_LIMIT):
    return strict_json(raw, max_bytes=limit, max_depth=64)


def _binding(actual, expected):
    if (type(actual) is not dict or set(actual) != _BINDING_KEYS
            or type(actual.get('schema_version')) is not int or actual['schema_version'] != 1
            or canonical_json(actual) != canonical_json(expected)):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_ATTEMPT_DRIFT')


def _validate(raw, *, expected_identity, expected_attempt_binding, historical=False):
    identity = _json(raw['identity.json'], _LIMITS['identity.json'])
    invocation = _json(raw['invocation.json'], _LIMITS['invocation.json'])
    plan = _json(raw['plan.json'], _LIMITS['plan.json'])
    if (canonical_json(identity) != canonical_json(expected_identity)
            or canonical_json(invocation.get('identity')) != canonical_json(identity)
            or invocation.get('rpc_plan_digest') != digest(plan)
            or canonical_json(invocation.get('rpc_plan')) != canonical_json(plan)
            or raw['stdin.bin'] != canonical_json(plan).encode()):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_IDENTITY_DRIFT')
    from .provider_app_server_business_identity import business_dispatch_invocation_boundaries_match
    if not business_dispatch_invocation_boundaries_match(
            invocation, expected_identity, historical=historical):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_INVOCATION_INVALID')
    spec, ready, stop = (_json(raw[name], _LIMITS[name]) for name in ('spec.json', 'ready.json', 'stop.json'))
    spec_keys = {'protocol_version', 'transport', 'nonce', 'argv', 'cwd', 'deadline_monotonic',
                 'output_limit', 'response_path', 'response_limit', 'pass_fds'}
    if (type(spec) is not dict or set(spec) != spec_keys or type(spec['protocol_version']) is not int
            or spec['protocol_version'] != 2 or spec['transport'] != 'app_server'
            or spec['argv'] != invocation['argv'] or spec['cwd'] != plan['cwd']
            or spec['response_path'] != invocation['outer_boundary']['response_path']
            or type(spec['response_limit']) is not int or not 1 <= spec['response_limit'] <= 65536
            or type(spec['output_limit']) is not int or not 1 <= spec['output_limit'] <= 2097152
            or type(spec['pass_fds']) is not list or len(spec['pass_fds']) > 8
            or len(set(spec['pass_fds'])) != len(spec['pass_fds'])
            or any(type(fd) is not int or fd < 3 for fd in spec['pass_fds'])):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_SPEC_INVALID')
    nonce = spec['nonce']
    if (type(nonce) is not str or re.fullmatch('[a-f0-9]{32}', nonce) is None
            or type(ready) is not dict or set(ready) != {'protocol_version', 'nonce', 'supervisor_pid', 'ready'}
            or type(ready['protocol_version']) is not int or ready['protocol_version'] != 2
            or ready['nonce'] != nonce or ready['ready'] is not True
            or type(ready['supervisor_pid']) is not int or ready['supervisor_pid'] <= 0):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_READY_INVALID')
    launched = _json(raw['launched.json']) if 'launched.json' in raw else None
    if stop.get('invocation_started') is True:
        if (type(launched) is not dict or set(launched) != {'protocol_version', 'nonce', 'cli_pid', 'supervisor_pid'}
                or type(launched['protocol_version']) is not int or launched['protocol_version'] != 2
                or launched['nonce'] != nonce or type(launched['cli_pid']) is not int
                or launched['cli_pid'] <= 0 or launched['cli_pid'] != stop.get('cli_pid')
                or launched['supervisor_pid'] != ready['supervisor_pid']):
            raise AppServerEvidenceError('APP_SERVER_EVIDENCE_LAUNCH_INVALID')
    elif launched is not None:
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_LAUNCH_INVALID')
    journal = {key: _json(raw['journal.' + key + '.json']) for key in ('binding', 'prepared', 'started', 'finished')}
    _binding(journal['binding'], expected_attempt_binding)
    entry, started, finished = journal['prepared'], journal['started'], journal['finished']
    entry_keys = {'schema_version', 'sequence', 'nonce', 'binding_digest', 'directory',
                  'deadline_monotonic', 'output_limit', 'phase', 'protocol_version', 'transport'}
    if (type(entry) is not dict or set(entry) != entry_keys or type(entry['schema_version']) is not int
            or entry['schema_version'] != 2 or entry['phase'] != 'app_server'
            or entry['nonce'] != nonce or entry['binding_digest'] != journal_digest(journal['binding'])
            or entry['deadline_monotonic'] != spec['deadline_monotonic']
            or entry['output_limit'] != spec['output_limit'] or type(entry['sequence']) is not int
            or not 1 <= entry['sequence'] <= MAX_STAGES or type(entry['directory']) is not str
            or not Path(entry['directory']).is_absolute() or '..' in Path(entry['directory']).parts
            or type(started) is not dict or set(started) != {'entry_digest', 'supervisor_pid'}
            or started['entry_digest'] != journal_digest(entry) or started['supervisor_pid'] != ready['supervisor_pid']
            or type(finished) is not dict or set(finished) != {'entry_digest', 'spawn_failed', 'stop'}
            or finished['entry_digest'] != journal_digest(entry) or finished['spawn_failed'] is not False
            or canonical_json(finished['stop']) != canonical_json(stop)):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_JOURNAL_DRIFT')
    try:
        validate_stop(stop, entry, started)
    except (ValueError, KeyError, TypeError) as exc:
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_STOP_UNCONFIRMED') from exc
    if stop['supervisor_pid'] != ready['supervisor_pid'] or len(raw['stdout.bin']) + len(raw['stderr.bin']) > spec['output_limit']:
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_STOP_DRIFT')
    try:
        capture = project_business_capture(plan=plan, stdout=raw['stdout.bin'], stderr=raw['stderr.bin'],
            response=raw.get('response.raw.json'), rpc_result=raw['rpc-result.json'], stop=stop,
            transport_version=invocation['transport_version'])
    except AppServerCaptureError as exc:
        raise AppServerEvidenceError(exc.code) from exc
    return {'capture': capture, 'invocation': invocation, 'plan': plan, 'stop': stop,
            'rpc_result': _json(raw['rpc-result.json']), 'journal': journal,
            'response': raw.get('response.raw.json')}


def persist_business_capture(*, supervision_dir, artifact_dir, invocation, expected_identity,
                             journal_dir, expected_attempt_binding):
    """Copy exact original records; callers retain private state on any failure."""
    source, target, journal_root = _path(supervision_dir), _path(artifact_dir), _path(journal_dir)
    raw = {'plan.json': canonical_json(invocation['rpc_plan']).encode(),
           'invocation.json': canonical_json(invocation).encode(),
           'identity.json': canonical_json(expected_identity).encode()}
    for name in ('spec.json', 'stdin.bin', 'ready.json', 'launched.json', 'stop.json',
                 'rpc-result.json', 'stdout.bin', 'stderr.bin', 'response.raw.json'):
        try:
            raw[name] = _read(source / name, _LIMITS[name])
        except FileNotFoundError:
            if name not in _OPTIONAL:
                raise AppServerEvidenceError('APP_SERVER_EVIDENCE_MISSING')
    raw['journal.binding.json'] = _read(journal_root / 'binding.json', _JSON_LIMIT)
    _binding(_json(raw['journal.binding.json']), expected_attempt_binding)
    stages = list(journal_root.iterdir())
    if len(stages) > MAX_STAGES + 1:
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_JOURNAL_LIMIT')
    matches = []
    for path in stages:
        if path.name == 'binding.json':
            continue
        if path.is_symlink() or not path.is_dir() or re.fullmatch('[0-9]{2}-[a-f0-9]{32}', path.name) is None:
            raise AppServerEvidenceError('APP_SERVER_EVIDENCE_JOURNAL_INVALID')
        entry_raw = _read(path / 'prepared.json', _JSON_LIMIT)
        entry = _json(entry_raw)
        if entry.get('directory') == str(source) and entry.get('nonce') == _json(raw['spec.json'], _LIMITS['spec.json'])['nonce']:
            matches.append((path, entry_raw))
    if len(matches) != 1:
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_JOURNAL_MISSING')
    path, entry_raw = matches[0]
    raw['journal.prepared.json'] = entry_raw
    for key in ('started', 'finished'):
        raw['journal.' + key + '.json'] = _read(path / (key + '.json'), _JSON_LIMIT)
    # Validate before claiming a persistent complete envelope. Partial copies
    # are retained if publication fails; the caller must fence and keep state.
    result = _validate(raw, expected_identity=expected_identity, expected_attempt_binding=expected_attempt_binding)
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = {}
    for name, data in sorted(raw.items()):
        if len(data) > _LIMITS[name]:
            raise AppServerEvidenceError('APP_SERVER_EVIDENCE_LIMIT')
        _atomic_bytes(target / name, data)
        manifest[name] = {'sha256': _sha(data), 'bytes': len(data)}
    envelope = {'evidence_version': evidence_version_for_transport(invocation['transport_version']),
                'transport_version': invocation['transport_version'],
                'identity_digest': digest(expected_identity), 'invocation_digest': digest(invocation),
                'plan_digest': digest(invocation['rpc_plan']), 'journal_binding_digest': digest(expected_attempt_binding),
                'capture': result['capture'], 'raw_manifest': manifest}
    _atomic_bytes(target / 'envelope.json', (canonical_json(envelope) + '\n').encode())
    return load_business_capture_evidence(target, expected_identity=expected_identity,
                                         expected_attempt_binding=expected_attempt_binding)


def load_business_capture_evidence(artifact_dir, *, expected_identity, expected_attempt_binding):
    """Recheck immutable saved raw files and original attempt binding after cleanup."""
    root = _path(artifact_dir)
    envelope = _json(_read(root / 'envelope.json', 131072), 131072)
    keys = {'evidence_version', 'transport_version', 'identity_digest', 'invocation_digest',
            'plan_digest', 'journal_binding_digest', 'capture', 'raw_manifest'}
    if (type(envelope) is not dict or set(envelope) != keys
            or envelope['evidence_version'] != evidence_version_for_transport(envelope['transport_version'])
            or envelope['identity_digest'] != digest(expected_identity)
            or envelope['journal_binding_digest'] != digest(expected_attempt_binding)
            or type(envelope['raw_manifest']) is not dict
            or set(envelope['raw_manifest']) - set(_LIMITS)
            or (set(_LIMITS) - _OPTIONAL) - set(envelope['raw_manifest'])
            or {path.name for path in root.iterdir()} != set(envelope['raw_manifest']) | {'envelope.json'}):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_ENVELOPE_INVALID')
    raw = {}
    for name, item in envelope['raw_manifest'].items():
        if (type(item) is not dict or set(item) != {'sha256', 'bytes'}
                or type(item['bytes']) is not int or not 0 <= item['bytes'] <= _LIMITS[name]
                or type(item['sha256']) is not str or re.fullmatch('[a-f0-9]{64}', item['sha256']) is None):
            raise AppServerEvidenceError('APP_SERVER_EVIDENCE_MANIFEST_INVALID')
        raw[name] = _read(root / name, _LIMITS[name])
        if len(raw[name]) != item['bytes'] or _sha(raw[name]) != item['sha256']:
            raise AppServerEvidenceError('APP_SERVER_EVIDENCE_RAW_DRIFT')
    if sum(map(len, raw.values())) > 4 * 1024 * 1024:
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_LIMIT')
    result = _validate(raw, expected_identity=expected_identity, expected_attempt_binding=expected_attempt_binding,
        historical=envelope['transport_version'] in {
            'codex_app_server_business_stdio_v3', 'codex_app_server_business_stdio_v4',
            'codex_app_server_business_stdio_v5', 'codex_app_server_business_stdio_v6'})
    if (envelope['transport_version'] != result['invocation']['transport_version']
            or envelope['invocation_digest'] != digest(result['invocation'])
            or envelope['plan_digest'] != digest(result['plan'])
            or canonical_json(envelope['capture']) != canonical_json(result['capture'])):
        raise AppServerEvidenceError('APP_SERVER_EVIDENCE_CAPTURE_DRIFT')
    return {'envelope': envelope, **result}
