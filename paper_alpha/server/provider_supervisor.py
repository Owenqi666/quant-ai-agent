"""Small POSIX supervisor for one local provider invocation.

Protocols v1 (exec) and v2 (App Server) are internal and separate from the
frozen provider domain contract. V2 drives a closed, bounded JSON-RPC dialogue;
it does not create a second process manager or infer success from CLI exit.
The worker owns only the control pipe's write end. EOF means that owner vanished;
this process owns the CLI child, its process group, deadline and inherited locks.
It neither resumes invocations nor acts on PIDs loaded from previous runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
import uuid

PROTOCOL_VERSION = 1
APP_SERVER_PROTOCOL_VERSION = 2
MAX_SPEC_BYTES = 1048576
MAX_RPC_FRAME_BYTES = 1048576
MAX_RPC_INPUT_BYTES = 262144
_STOP_SIGNAL = False


def _signal_stop(_number, _frame):
    global _STOP_SIGNAL
    _STOP_SIGNAL = True


def _private_bytes(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('not a bounded private regular file')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('file limit exceeded')
    return data


def _publish(path: Path, value: dict):
    data = (json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode()
    _publish_bytes(path, data)


def _publish_bytes(path: Path, data: bytes):
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_spec(path: Path) -> dict:
    spec = json.loads(_private_bytes(path, MAX_SPEC_BYTES))
    expected = {'protocol_version', 'nonce', 'argv', 'cwd', 'deadline_monotonic',
                'output_limit', 'response_path', 'response_limit', 'pass_fds'}
    if not isinstance(spec, dict) or type(spec.get('protocol_version')) is not int:
        raise ValueError('invalid supervisor protocol')
    if spec['protocol_version'] == APP_SERVER_PROTOCOL_VERSION:
        expected.add('transport')
        if spec.get('transport') != 'app_server' or spec.get('response_path') is None:
            raise ValueError('invalid App Server transport')
    elif spec['protocol_version'] != PROTOCOL_VERSION:
        raise ValueError('unsupported supervisor protocol')
    if set(spec) != expected:
        raise ValueError('invalid supervisor protocol')
    if not isinstance(spec['nonce'], str) or len(spec['nonce']) != 32 or any(c not in '0123456789abcdef' for c in spec['nonce']):
        raise ValueError('invalid nonce')
    if not isinstance(spec['argv'], list) or not spec['argv'] or any(not isinstance(v, str) or not v or '\0' in v for v in spec['argv']):
        raise ValueError('invalid argument array')
    if not isinstance(spec['deadline_monotonic'], (int, float)) or not math.isfinite(spec['deadline_monotonic']):
        raise ValueError('invalid monotonic deadline')
    if spec['deadline_monotonic'] > time.monotonic() + 605:
        raise ValueError('deadline exceeds provider limits')
    for key, high in (('output_limit', 8388608), ('response_limit', 65536)):
        if type(spec[key]) is not int or not 1 <= spec[key] <= high:
            raise ValueError('invalid byte limit')
    if not isinstance(spec['pass_fds'], list) or any(type(fd) is not int or fd < 3 for fd in spec['pass_fds']):
        raise ValueError('invalid lease descriptors')
    if not isinstance(spec['cwd'], str) or not Path(spec['cwd']).is_absolute():
        raise ValueError('invalid working directory')
    if spec['response_path'] is not None:
        response = Path(spec['response_path'])
        if response.parent != Path(spec['cwd']) or response.name != 'response.raw.json':
            raise ValueError('response must be in the isolated working directory')
    return spec


def _group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_owned_group(process: subprocess.Popen, diagnostics: dict | None = None) -> tuple[bool, bool]:
    """Signal only the freshly spawned group owned by this live supervisor."""
    if diagnostics is not None:
        return _terminate_app_server_owned_group(process, diagnostics)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    until = time.monotonic() + .08
    while time.monotonic() < until:
        process.poll()
        if not _group_exists(process.pid):
            break
        time.sleep(.005)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False, False
    until = time.monotonic() + 2
    while _group_exists(process.pid) and time.monotonic() < until:
        time.sleep(.01)
    return process.returncode is not None, not _group_exists(process.pid)


def _terminate_app_server_owned_group(process: subprocess.Popen, diagnostics: dict) -> tuple[bool, bool]:
    """Reap even when a dying macOS group briefly denies a zero-signal probe.

    EPERM is unknown, never an absence proof. Only a later ESRCH observation of
    this freshly owned group, plus wait/reap, can confirm termination. Persistent
    permission failure stays fenced within the original finite cleanup bounds.
    """
    def signal_group(number, phase, sent_key):
        diagnostics['phase'] = phase
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            diagnostics['error_errno'] = exc.errno
            diagnostics['error_count'] += 1
            return None
        diagnostics[sent_key] = True
        return True

    def observe(phase):
        diagnostics['phase'] = phase
        try:
            return _group_exists(process.pid)
        except PermissionError as exc:
            diagnostics['error_errno'] = exc.errno
            diagnostics['error_count'] += 1
            return None

    absence = signal_group(signal.SIGTERM, 'term', 'term_sent') is False
    until = time.monotonic() + .08
    while not absence and time.monotonic() < until:
        process.poll()
        absence = observe('term_group_observation') is False
        if not absence:
            time.sleep(.005)
    if not absence:
        absence = signal_group(signal.SIGKILL, 'kill', 'kill_sent') is False
    diagnostics['phase'] = 'reap'
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False, False
    until = time.monotonic() + 2
    while not absence and time.monotonic() < until:
        absence = observe('final_group_observation') is False
        if not absence:
            time.sleep(.01)
    return process.returncode is not None, absence


def supervise(spec_path: Path, parent_fd: int) -> int:
    spec = _load_spec(spec_path)
    root = spec_path.parent
    nonce = spec['nonce']
    protocol_version = spec['protocol_version']
    is_app_server = protocol_version == APP_SERVER_PROTOCOL_VERSION
    stdin_data = _private_bytes(root / 'stdin.bin', 131072)
    stdout_path, stderr_path = root / 'stdout.bin', root / 'stderr.bin'
    process = None
    started = time.monotonic()
    reason = None
    leader_reaped = group_absent = True
    streams = {}
    totals = {'stdout': 0, 'stderr': 0}
    response_saved = False
    response_error = None
    protocol = None
    rpc_buffer = bytearray()
    rpc_pending = bytearray()
    rpc_input_bytes = 0
    rpc_eof = False
    rpc_result_sha256 = None
    cleanup_diagnostic = {'phase': None, 'term_sent': False, 'kill_sent': False,
                          'error_errno': None, 'error_count': 0, 'remaining_streams': []}
    selector = selectors.DefaultSelector()
    os.set_blocking(parent_fd, False)
    selector.register(parent_fd, selectors.EVENT_READ, 'parent')
    output = {'stdout': stdout_path.open('xb', buffering=0),
              'stderr': stderr_path.open('xb', buffering=0)}
    signal.signal(signal.SIGTERM, _signal_stop)
    signal.signal(signal.SIGINT, _signal_stop)
    _publish(root / 'ready.json', {'protocol_version': protocol_version, 'nonce': nonce,
                                  'supervisor_pid': os.getpid(), 'ready': True})

    def parent_message() -> str | None:
        try:
            message = os.read(parent_fd, 64)
        except BlockingIOError:
            return None
        if not message:
            return 'PARENT_LOST'
        return 'CANCELLED' if message == b'C' else 'SUPERVISOR_PROTOCOL_ERROR'

    def close_stdin():
        if process.stdin is not None and not process.stdin.closed:
            try:
                selector.unregister(process.stdin)
            except KeyError:
                pass
            process.stdin.close()

    def queue_frames(frames):
        nonlocal rpc_input_bytes
        for frame in frames:
            if (not isinstance(frame, bytes) or not frame.endswith(b'\n') or
                    frame.count(b'\n') != 1 or len(frame) > MAX_RPC_FRAME_BYTES):
                raise ValueError('invalid outbound App Server frame')
            rpc_input_bytes += len(frame)
            if rpc_input_bytes > MAX_RPC_INPUT_BYTES:
                raise ValueError('App Server input limit exceeded')
            rpc_pending.extend(frame)
        if rpc_pending and process.stdin is not None and not process.stdin.closed:
            try:
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            except KeyError:
                pass

    def protocol_eof():
        nonlocal rpc_eof, reason
        if rpc_eof:
            return
        rpc_eof = True
        if rpc_buffer and reason is None:
            reason = 'APP_SERVER_PARTIAL_FRAME'
        if protocol is not None and reason is None:
            try:
                protocol.finish_eof()
            except Exception as exc:
                reason = getattr(exc, 'code', 'APP_SERVER_PROTOCOL_ERROR')

    def read_output(key):
        nonlocal reason
        stream = key.fileobj
        try:
            chunk = os.read(stream.fileno(), 4096)
        except BlockingIOError:
            return
        if not chunk:
            selector.unregister(stream)
            streams.pop(key.data, None)
            stream.close()
            if is_app_server and key.data == 'stdout':
                protocol_eof()
            return
        available = max(0, spec['output_limit'] - sum(totals.values()))
        output[key.data].write(chunk[:available])
        totals[key.data] += min(len(chunk), available)
        if len(chunk) > available and reason is None:
            reason = 'OUTPUT_LIMIT'
        if is_app_server and key.data == 'stdout' and reason is None:
            rpc_buffer.extend(chunk)
            try:
                while b'\n' in rpc_buffer:
                    index = rpc_buffer.index(b'\n') + 1
                    if index > min(MAX_RPC_FRAME_BYTES, spec['output_limit']):
                        reason = 'APP_SERVER_FRAME_LIMIT'
                        break
                    line = bytes(rpc_buffer[:index])
                    del rpc_buffer[:index]
                    queue_frames(protocol.feed(line))
                    if protocol.complete:
                        if rpc_pending:
                            reason = 'APP_SERVER_PROTOCOL_ERROR'
                            break
                        close_stdin()
                if len(rpc_buffer) > min(MAX_RPC_FRAME_BYTES, spec['output_limit']):
                    reason = 'APP_SERVER_FRAME_LIMIT'
            except Exception as exc:
                reason = getattr(exc, 'code', 'APP_SERVER_PROTOCOL_ERROR')

    try:
        if is_app_server:
            # The supervisor may be executed as a file, without a package root
            # on sys.path. The protocol module itself is standalone pure code.
            try:
                from .provider_app_server_protocol import AppServerProtocol
            except ImportError:
                from provider_app_server_protocol import AppServerProtocol
            plan = json.loads(stdin_data)
            canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode()
            if stdin_data != canonical:
                raise ValueError('App Server plan is not canonical')
            if not isinstance(plan, dict) or plan.get('cwd') != spec['cwd']:
                raise ValueError('App Server working directory differs')
            protocol = AppServerProtocol(plan, response_limit=spec['response_limit'])
            initial_frames = protocol.start()
        # Check owner liveness before any CLI spawn, including the launch race.
        reason = parent_message()
        if (reason is None and is_app_server and
                (Path(spec['response_path']).exists() or Path(spec['response_path']).is_symlink())):
            reason = 'INVALID_RESPONSE_FILE'
        if reason is None and time.monotonic() >= spec['deadline_monotonic']:
            reason = 'TIMEOUT'
        if reason is None and _STOP_SIGNAL:
            reason = 'SUPERVISOR_INTERRUPTED'
        if reason is None:
            try:
                process = subprocess.Popen(spec['argv'], cwd=spec['cwd'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True, pass_fds=tuple(spec['pass_fds']))
            except OSError:
                reason = 'CLI_UNAVAILABLE'
            else:
                leader_reaped = group_absent = False
                _publish(root / 'launched.json', {'protocol_version': protocol_version,
                    'nonce': nonce, 'cli_pid': process.pid, 'supervisor_pid': os.getpid()})
                for label, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    streams[label] = stream
                    selector.register(stream, selectors.EVENT_READ, label)
                if is_app_server:
                    os.set_blocking(process.stdin.fileno(), False)
                    queue_frames(initial_frames)
                elif stdin_data:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
                else:
                    process.stdin.close()
                offset = 0
                while reason is None:
                    if time.monotonic() >= spec['deadline_monotonic']:
                        reason = 'TIMEOUT'
                        break
                    if _STOP_SIGNAL:
                        reason = 'SUPERVISOR_INTERRUPTED'
                        break
                    if spec['response_path'] is not None:
                        try:
                            info = Path(spec['response_path']).lstat()
                            if is_app_server or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                                reason = 'INVALID_RESPONSE_FILE'
                            elif info.st_size > spec['response_limit']:
                                reason = 'OUTPUT_LIMIT'
                        except FileNotFoundError:
                            pass
                    if reason is not None:
                        break
                    for key, _ in selector.select(.01):
                        if key.data == 'parent':
                            reason = parent_message()
                        elif key.data == 'stdin':
                            if is_app_server:
                                try:
                                    written = os.write(process.stdin.fileno(), rpc_pending[:4096])
                                    del rpc_pending[:written]
                                except BlockingIOError:
                                    continue
                                except (BrokenPipeError, OSError):
                                    reason = reason or 'APP_SERVER_STDIN_CLOSED'
                                    close_stdin()
                                else:
                                    if not rpc_pending:
                                        selector.unregister(process.stdin)
                            else:
                                try:
                                    offset += os.write(process.stdin.fileno(), stdin_data[offset:offset + 4096])
                                except (BrokenPipeError, OSError):
                                    offset = len(stdin_data)
                                if offset >= len(stdin_data):
                                    selector.unregister(process.stdin)
                                    process.stdin.close()
                        else:
                            read_output(key)
                    if process.poll() is not None:
                        break
    except BaseException:
        reason = reason or 'SUPERVISOR_FAILED'
    finally:
        if process is not None:
            try:
                leader_reaped, group_absent = _terminate_owned_group(process,
                    diagnostics=cleanup_diagnostic if is_app_server else None)
            except (OSError, ValueError) as exc:
                leader_reaped = process.poll() is not None
                group_absent = False
                if is_app_server:
                    cleanup_diagnostic['error_errno'] = exc.errno if isinstance(exc, OSError) else None
                    cleanup_diagnostic['error_count'] += 1
            if process.stdin is not None and not process.stdin.closed:
                try:
                    selector.unregister(process.stdin)
                except KeyError:
                    pass
                process.stdin.close()
            # Drain finite pipe remnants only after group cleanup. Raw bytes are
            # retained even when the owner has vanished or the result is invalid.
            drain_until = time.monotonic() + .3
            while streams and time.monotonic() < drain_until:
                for key, _ in selector.select(.01):
                    if key.data in ('stdout', 'stderr'):
                        read_output(key)
            if streams:
                group_absent = False
                if is_app_server:
                    cleanup_diagnostic['remaining_streams'] = sorted(streams)
            for stream in list(streams.values()):
                stream.close()
            if not (leader_reaped and group_absent):
                reason = 'PROCESS_CLEANUP_FAILED'
        if is_app_server:
            protocol_eof()
            if reason is None and (protocol is None or not protocol.complete):
                reason = 'APP_SERVER_INCOMPLETE'
            if reason is None and process is not None and process.returncode != 0:
                reason = 'APP_SERVER_EXIT_NONZERO'
            if (reason is None and protocol is not None and leader_reaped and group_absent):
                try:
                    response_bytes = protocol.response
                    if not isinstance(response_bytes, bytes) or len(response_bytes) > spec['response_limit']:
                        raise ValueError('invalid App Server response')
                    _publish_bytes(Path(spec['response_path']), response_bytes)
                except (OSError, ValueError):
                    response_error = 'INVALID_RESPONSE_FILE'
            try:
                summary = protocol.summary() if protocol is not None else {'protocol_version': 1, 'completed': False}
                summary['supervision'] = cleanup_diagnostic
                data = (json.dumps(summary, sort_keys=True, allow_nan=False) + '\n').encode()
                if len(data) > 65536:
                    raise ValueError('App Server summary exceeds limit')
                _publish_bytes(root / 'rpc-result.json', data)
                rpc_result_sha256 = hashlib.sha256(data).hexdigest()
            except (OSError, ValueError):
                reason = reason or 'APP_SERVER_SUMMARY_INVALID'
        if (spec['response_path'] is not None and leader_reaped and group_absent and
                (not is_app_server or (reason is None and response_error is None))):
            try:
                response_bytes = _private_bytes(Path(spec['response_path']), spec['response_limit'])
                with (root / 'response.raw.json').open('xb') as stream:
                    stream.write(response_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                response_saved = True
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                response_error = 'INVALID_RESPONSE_FILE'
        for stream in output.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
        selector.close()
        os.close(parent_fd)
        stopped = bool(leader_reaped and group_absent)
        stop = {'protocol_version': protocol_version, 'nonce': nonce,
            'supervisor_pid': os.getpid(), 'cli_pid': process.pid if process else None,
            'invocation_started': process is not None, 'started_monotonic': started,
            'finished_monotonic': time.monotonic(), 'deadline_monotonic': spec['deadline_monotonic'],
            'exit_code': process.returncode if process else None, 'reason': reason,
            'leader_reaped': leader_reaped, 'group_absent': group_absent,
            'process_stopped': stopped, 'stdout_bytes': totals['stdout'], 'stderr_bytes': totals['stderr'],
            'stdout_sha256': hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
            'stderr_sha256': hashlib.sha256(stderr_path.read_bytes()).hexdigest(),
            'response_saved': response_saved, 'response_error': response_error,
            'response_sha256': hashlib.sha256((root / 'response.raw.json').read_bytes()).hexdigest() if response_saved else None}
        if is_app_server:
            stop.update(transport='app_server', rpc_completed=bool(protocol is not None and protocol.complete),
                        rpc_result_sha256=rpc_result_sha256)
        _publish(root / 'stop.json', stop)
    # Inherited leases are deliberately held through durable stop confirmation.
    return 0 if stopped else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--parent-fd', type=int, required=True)
    args = parser.parse_args(argv)
    return supervise(args.spec, args.parent_fd)


if __name__ == '__main__':
    raise SystemExit(main())
