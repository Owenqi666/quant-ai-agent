"""Bounded, non-credential recovery evidence for the existing CLI supervisor.

The context is installed by ProposalWorker, never supplied over HTTP. Records
precede spawn so SIGKILL cannot erase the association with a fenced attempt.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
import hashlib
import math
import os
from pathlib import Path
import platform
import re
import stat

from ..storage import atomic_json, digest
from .provider_contract import strict_json

ACTIVE_JOURNAL = ContextVar('provider_supervision_journal', default=None)
MAX_STAGES = 32


def host_boot_identity():
    """Native metadata only; no shell, credentials, stable raw host ID or probing."""
    try:
        if platform.system() == 'Linux':
            host = Path('/etc/machine-id').read_bytes().strip()
            boot = Path('/proc/sys/kernel/random/boot_id').read_bytes().strip()
        elif platform.system() == 'Darwin':
            lib = ctypes.CDLL(None)
            host_buffer = (ctypes.c_ubyte * 16)()
            class Timespec(ctypes.Structure):
                _fields_ = [('tv_sec', ctypes.c_long), ('tv_nsec', ctypes.c_long)]
            timeout = Timespec(1, 0)
            if lib.gethostuuid(ctypes.byref(host_buffer), ctypes.byref(timeout)) != 0:
                raise OSError('Host identity unavailable')
            host = bytes(host_buffer)
            buffer = ctypes.create_string_buffer(128)
            size = ctypes.c_size_t(len(buffer))
            if lib.sysctlbyname(b'kern.bootsessionuuid', buffer, ctypes.byref(size), None, 0) != 0:
                raise OSError('Boot identity unavailable')
            boot = buffer.raw[:size.value].rstrip(b'\0')
        else:
            return None
        if not host or not boot or len(host) > 128 or len(boot) > 128:
            return None
        return {'host': hashlib.sha256(host).hexdigest(), 'boot': hashlib.sha256(boot).hexdigest()}
    except (OSError, AttributeError, ValueError):
        return None


def read_metadata(path, limit=65536):
    path = Path(path)
    if '..' in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('Unsafe supervision metadata path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('Invalid bounded supervision metadata')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(data) != before.st_size or len(data) > limit or
            (before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_ino, after.st_size, after.st_mtime_ns)):
        raise ValueError('Supervision metadata changed')
    return strict_json(data, max_bytes=limit)


def _write(path, payload):
    if path.exists() or path.is_symlink():
        raise ValueError('Supervision journal is immutable')
    atomic_json(path, payload)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SupervisionJournal:
    def __init__(self, root, binding):
        self.root = Path(root)
        if any(item.is_symlink() for item in (self.root, *self.root.parents)):
            raise ValueError('Unsafe supervision journal')
        self.root.mkdir(mode=0o700)
        self.binding = {**binding, 'schema_version': 1, 'host_boot': host_boot_identity()}
        _write(self.root / 'binding.json', self.binding)
        self.sequence = 0

    def prepare(self, *, nonce, directory, deadline, output_limit, phase,
                protocol_version=1, transport=None):
        if (type(protocol_version) is not int or protocol_version not in {1, 2} or
                (protocol_version == 1 and (transport is not None or phase not in {'version', 'auth', 'cli'})) or
                (protocol_version == 2 and (transport != 'app_server' or phase != 'app_server'))):
            raise ValueError('Invalid supervision journal transport')
        self.sequence += 1
        if self.sequence > MAX_STAGES:
            raise ValueError('Supervision journal stage limit')
        entry = {'schema_version': 1, 'sequence': self.sequence, 'nonce': nonce,
                 'binding_digest': digest(self.binding), 'directory': str(directory.resolve()),
                 'deadline_monotonic': deadline, 'output_limit': output_limit, 'phase': phase}
        if protocol_version == 2:
            entry.update(schema_version=2, protocol_version=2, transport='app_server')
        path = self.root / f'{self.sequence:02d}-{nonce}'
        path.mkdir(mode=0o700)
        _write(path / 'prepared.json', entry)
        return path, entry

    def started(self, stage, supervisor_pid):
        path, entry = stage
        _write(path / 'started.json', {'entry_digest': digest(entry), 'supervisor_pid': supervisor_pid})

    def finish(self, stage, *, stopped, spawn_failed=False):
        if not stopped:
            return
        path, entry = stage
        if spawn_failed:
            _write(path / 'finished.json', {'entry_digest': digest(entry), 'spawn_failed': True, 'stop': None})
            return
        stop = read_metadata(Path(entry['directory']) / 'stop.json')
        validate_stop(stop, entry, read_metadata(path / 'started.json'))
        # stop.json contains only supervision metadata. It never includes argv,
        # stdin, stdout/stderr bytes, native authentication or response content.
        _write(path / 'finished.json', {'entry_digest': digest(entry), 'spawn_failed': False, 'stop': stop})


def validate_stop(stop, entry, started=None):
    fields = {'protocol_version', 'nonce', 'supervisor_pid', 'cli_pid', 'invocation_started',
              'started_monotonic', 'finished_monotonic', 'deadline_monotonic', 'exit_code',
              'reason', 'leader_reaped', 'group_absent', 'process_stopped', 'stdout_bytes',
              'stderr_bytes', 'stdout_sha256', 'stderr_sha256', 'response_saved',
              'response_error', 'response_sha256'}
    version = entry.get('schema_version')
    if type(version) is not int or version not in {1, 2}:
        raise ValueError('Unsupported journal version')
    if version == 2:
        if (entry.get('protocol_version') != 2 or type(entry.get('protocol_version')) is not int or
                entry.get('transport') != 'app_server' or entry.get('phase') != 'app_server'):
            raise ValueError('Invalid journal transport identity')
        fields |= {'transport', 'rpc_completed', 'rpc_result_sha256'}
    elif any(key in entry for key in ('protocol_version', 'transport')):
        raise ValueError('Legacy journal cannot adopt a new transport')
    if not isinstance(stop, dict) or set(stop) != fields:
        raise ValueError('Incomplete stop metadata')
    if (type(stop['protocol_version']) is not int or stop['protocol_version'] != version or
            stop['nonce'] != entry['nonce'] or stop['deadline_monotonic'] != entry['deadline_monotonic']):
        raise ValueError('Stop identity differs')
    if type(stop['supervisor_pid']) is not int or stop['supervisor_pid'] <= 0:
        raise ValueError('Invalid supervisor identity')
    if started is not None and (started.get('entry_digest') != digest(entry) or
                               started.get('supervisor_pid') != stop['supervisor_pid'] or
                               set(started) != {'entry_digest', 'supervisor_pid'}):
        raise ValueError('Supervisor changed')
    for key in ('invocation_started', 'leader_reaped', 'group_absent', 'process_stopped', 'response_saved'):
        if type(stop[key]) is not bool:
            raise ValueError('Invalid stop state')
    if not all(stop[key] for key in ('leader_reaped', 'group_absent', 'process_stopped')):
        raise ValueError('Process stop is unconfirmed')
    if stop['invocation_started']:
        if type(stop['cli_pid']) is not int or stop['cli_pid'] <= 0:
            raise ValueError('Invalid child identity')
    elif stop['cli_pid'] is not None:
        raise ValueError('Unexpected child identity')
    if stop['exit_code'] is not None and type(stop['exit_code']) is not int:
        raise ValueError('Invalid exit code')
    for key in ('started_monotonic', 'finished_monotonic', 'deadline_monotonic'):
        if type(stop[key]) not in (int, float) or not math.isfinite(stop[key]):
            raise ValueError('Invalid stop clock')
    if stop['finished_monotonic'] < stop['started_monotonic']:
        raise ValueError('Stop clock differs')
    for key in ('stdout_bytes', 'stderr_bytes'):
        if type(stop[key]) is not int or not 0 <= stop[key] <= entry['output_limit']:
            raise ValueError('Invalid stop byte count')
    for key in ('stdout_sha256', 'stderr_sha256'):
        if not isinstance(stop[key], str) or not re.fullmatch('[0-9a-f]{64}', stop[key]):
            raise ValueError('Invalid stop digest')
    if stop['response_sha256'] is not None and not re.fullmatch('[0-9a-f]{64}', str(stop['response_sha256'])):
        raise ValueError('Invalid response digest')
    if stop['response_saved'] != (stop['response_sha256'] is not None):
        raise ValueError('Response presence differs')
    for key in ('reason', 'response_error'):
        if stop[key] is not None and (not isinstance(stop[key], str) or not re.fullmatch('[A-Z_]{1,80}', stop[key])):
            raise ValueError('Invalid stop reason')
    if version == 2:
        if (stop['transport'] != 'app_server' or type(stop['rpc_completed']) is not bool or
                not isinstance(stop['rpc_result_sha256'], str) or
                not re.fullmatch('[0-9a-f]{64}', stop['rpc_result_sha256'])):
            raise ValueError('Invalid App Server stop metadata')
        if stop['response_saved'] and (not stop['rpc_completed'] or stop['reason'] is not None or
                                       stop['exit_code'] != 0 or not stop['process_stopped']):
            raise ValueError('App Server result lacks completed owned execution')


@contextmanager
def journal_scope(root, binding):
    journal = SupervisionJournal(root, binding)
    token = ACTIVE_JOURNAL.set(journal)
    try:
        yield journal
    finally:
        ACTIVE_JOURNAL.reset(token)


def termination_proof(root, value, workspace_identity, origin=None):
    """Read only metadata; never inspect credentials or signal a saved PID."""
    attempt = value['attempts'][-1]
    folder = Path(root) / 'proposals' / value['id'] / 'attempts' / attempt['id'] / 'supervision'
    current_boot = host_boot_identity()
    boot_proof = None
    # New attempts persist this origin in the same transaction as charging,
    # before prepared input or any subprocess can exist. Legacy rows lack it.
    if origin is not None:
        expected = {'schema_version', 'proposal_id', 'attempt_id', 'attempt_charged_digest',
                    'fencing_token', 'workspace_identity', 'host_boot'}
        anchors = {item['attempt_digest'] for item in value['events']
                   if item.get('attempt_id') == attempt['id'] and item['kind'] == 'attempt_charged'}
        origin_anchors = {item.get('origin_digest') for item in value['events']
                          if item.get('attempt_id') == attempt['id'] and item['kind'] == 'attempt_charged'}
        if (set(origin) != expected or origin['schema_version'] != 1 or
                origin['proposal_id'] != value['id'] or origin['attempt_id'] != attempt['id'] or
                origin['fencing_token'] != attempt['fencing_token'] or origin['attempt_charged_digest'] not in anchors or
                digest(origin) not in origin_anchors):
            return {'confirmed': False, 'reason': 'ORIGIN_IDENTITY_INVALID'}
        old_boot = origin['host_boot']
        if (isinstance(old_boot, dict) and set(old_boot) == {'host', 'boot'} and current_boot is not None and
                old_boot['host'] == current_boot['host'] and old_boot['boot'] != current_boot['boot']):
            boot_proof = {'confirmed': True, 'reason': 'SAME_HOST_NEW_BOOT',
                          'origin_digest': digest(origin), 'current_boot_digest': digest(current_boot)}
    if boot_proof is not None and not folder.is_symlink():
        if not folder.exists():
            return boot_proof
        # A kill between mkdir and the first atomic binding write leaves no
        # journal yet. The transactionally anchored boot origin is sufficient;
        # an existing conclusion is never ignored as an incomplete write.
        if folder.is_dir() and not (folder / 'binding.json').exists():
            children = list(folder.iterdir())
            if len(children) <= MAX_STAGES and not any(child.is_symlink() or (child / 'finished.json').exists()
                                                        for child in children if child.is_dir()):
                return boot_proof
    try:
        binding = read_metadata(folder / 'binding.json')
        if (set(binding) != {'schema_version', 'workspace_identity', 'proposal_id', 'attempt_id',
                            'fencing_token', 'prepared_digest', 'config_digest', 'attempt_started_digest', 'host_boot'} or
                type(binding.get('schema_version')) is not int or binding['schema_version'] != 1 or
                binding.get('proposal_id') != value['id'] or binding.get('attempt_id') != attempt['id'] or
                binding.get('fencing_token') != attempt['fencing_token'] or
                binding.get('prepared_digest') != attempt['prepared_digest'] or
                binding.get('config_digest') != digest(value['config'])):
            raise ValueError('Journal belongs to another attempt')
        anchors = {item['attempt_digest'] for item in value['events']
                   if item.get('attempt_id') == attempt['id'] and item['kind'] == 'invocation_prepared'}
        if binding.get('attempt_started_digest') not in anchors:
            raise ValueError('Journal is not bound to the prepared attempt')
        if origin is None or binding['host_boot'] != origin['host_boot'] or binding['workspace_identity'] != origin['workspace_identity']:
            raise ValueError('Journal differs from the anchored attempt origin')
        same_workspace = binding.get('workspace_identity') == workspace_identity
        old_boot = binding.get('host_boot')
        rebooted = boot_proof is not None
        stages = sorted(item for item in folder.iterdir() if item.name != 'binding.json')
        if not stages and boot_proof is not None:
            return boot_proof
        if not stages or len(stages) > MAX_STAGES:
            raise ValueError('No complete spawn journal')
        proofs = []
        for sequence, path in enumerate(stages, 1):
            if (boot_proof is not None and path.is_dir() and not path.is_symlink() and
                    re.fullmatch(r'[0-9]{2}-[0-9a-f]{32}', path.name) and
                    not (path / 'prepared.json').exists() and not (path / 'finished.json').exists()):
                proofs.append({'stage': sequence, 'kind': 'same_host_new_boot', 'digest': digest(current_boot)})
                continue
            entry = read_metadata(path / 'prepared.json')
            entry_fields = {'schema_version', 'sequence', 'nonce', 'binding_digest', 'directory',
                            'deadline_monotonic', 'output_limit', 'phase'}
            if type(entry.get('schema_version')) is int and entry['schema_version'] == 2:
                entry_fields |= {'protocol_version', 'transport'}
                phase_valid = (type(entry.get('protocol_version')) is int and entry['protocol_version'] == 2 and
                               entry.get('transport') == 'app_server' and entry.get('phase') == 'app_server')
            else:
                phase_valid = entry.get('phase') in {'version', 'auth', 'cli'}
            if (set(entry) != entry_fields or not phase_valid or
                    type(entry.get('schema_version')) is not int or entry['schema_version'] not in {1, 2} or
                    type(entry.get('sequence')) is not int or entry['sequence'] != sequence or
                    entry.get('binding_digest') != digest(binding) or
                    not re.fullmatch('[0-9a-f]{32}', str(entry.get('nonce'))) or
                    path.name != f"{sequence:02d}-{entry['nonce']}"):
                raise ValueError('Incomplete journal sequence')
            directory = Path(entry['directory'])
            if (not directory.is_absolute() or '..' in directory.parts or
                    type(entry['deadline_monotonic']) not in (int, float) or not math.isfinite(entry['deadline_monotonic']) or
                    type(entry['output_limit']) is not int or not 0 <= entry['output_limit'] <= 8 * 1024 * 1024 or
                    not phase_valid):
                raise ValueError('Invalid journal spawn metadata')
            completed = path / 'finished.json'
            if completed.exists():
                final = read_metadata(completed)
                if (set(final) != {'entry_digest', 'spawn_failed', 'stop'} or final['entry_digest'] != digest(entry) or
                        type(final['spawn_failed']) is not bool):
                    raise ValueError('Journal conclusion differs')
                if final['spawn_failed'] is True and final['stop'] is None:
                    proofs.append({'stage': sequence, 'kind': 'spawn_failed', 'digest': digest(final)})
                    continue
                started = read_metadata(path / 'started.json')
                validate_stop(final['stop'], entry, started)
                proofs.append({'stage': sequence, 'kind': 'saved_stop', 'digest': digest(final)})
                continue
            if rebooted:
                proofs.append({'stage': sequence, 'kind': 'same_host_new_boot', 'digest': digest(current_boot)})
                continue
            # A restored copy must not dereference a previous workspace's temp
            # path; copied finished metadata or same-host reboot is required.
            if not same_workspace or (old_boot is not None and current_boot is not None and current_boot != old_boot):
                raise ValueError('Historical private reference cannot be followed')
            stop = read_metadata(Path(entry['directory']) / 'stop.json')
            started = read_metadata(path / 'started.json') if (path / 'started.json').exists() else None
            validate_stop(stop, entry, started)
            proofs.append({'stage': sequence, 'kind': 'supervisor_stop', 'digest': digest(stop)})
        return {'confirmed': True, 'reason': 'BOUND_STOP_EVIDENCE', 'binding_digest': digest(binding), 'stages': proofs}
    except (OSError, ValueError, KeyError, TypeError):
        return {'confirmed': False, 'reason': 'STOP_EVIDENCE_UNCONFIRMED'}
