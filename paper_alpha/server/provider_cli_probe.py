"""Observe real CLI request construction against a non-forwarding loopback stub.

This is transport evidence, never backend/model or production isolation proof.
Only allowlisted structural fields leave the private invocation directory.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Literal
import uuid

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from .codex_cli_provider import _Capture, _atomic_bytes, _bounded_private_file, _run_bounded
from .provider_contract import digest, strict_json
from .provider_schema import CONTRACT_ID, ProviderConfig

IDENTIFIER = r'^[A-Za-z0-9_.:-]{1,128}$'
DIGEST = r'^[a-f0-9]{64}$'
SYNTHETIC_PROMPT = 'Local transport inspection only. Return a JSON object for the supplied schema. No research data or real model is involved.'
MAX_REQUEST_BYTES = 1048576
REQUEST_FIELDS = {'model', 'instructions', 'input', 'tools', 'tool_choice', 'parallel_tool_calls',
                  'reasoning', 'text', 'stream', 'store', 'include', 'metadata', 'service_tier',
                  'prompt_cache_key', 'prompt_cache_retention', 'previous_response_id',
                  'max_output_tokens', 'temperature', 'top_p', 'truncation', 'background',
                  'conversation', 'safety_identifier', 'client_metadata'}


class ClientMetadataShape(AuthorModel):
    """Only the envelope shape is observed; metadata values are never retained."""
    object_observed: bool
    entry_count: int | None = Field(default=None, ge=0, le=32)
    values_recorded: Literal[False] = False


class ObservedTool(AuthorModel):
    type: str = Field(pattern=IDENTIFIER)
    name: str | None = Field(default=None, pattern=IDENTIFIER)


class LocalCliObservation(AuthorModel):
    schema_version: Literal[1] = 1
    evidence_kind: Literal['local_cli_probe', 'fixture']
    profile_digest: str = Field(pattern=DIGEST)
    status: Literal['blocked', 'failed', 'unknown']
    cli_version: str | None = Field(default=None, max_length=128)
    request_count: int = Field(ge=0, le=4)
    request_observed: bool
    request_fields: list[str] = Field(max_length=40)
    unknown_request_fields: list[str] = Field(default_factory=list, max_length=40)
    client_metadata_shape: ClientMetadataShape | None = None
    requested_model: str | None = Field(default=None, max_length=128)
    tools: list[ObservedTool] = Field(max_length=512)
    tool_count: int | None = Field(default=None, ge=0, le=512)
    schema_observed: bool
    schema_digest: str | None = Field(default=None, pattern=DIGEST)
    schema_matches: bool | None = None
    process_stopped: bool
    transport_error: str | None = Field(default=None, pattern=r'^[A-Z_]{1,80}$')
    blockers: list[str] = Field(max_length=40)
    real_model_invocations: Literal[0] = 0
    remote_backend_compatibility: Literal['unknown'] = 'unknown'
    production_tool_boundary: Literal['unverified'] = 'unverified'
    endpoint_kind: Literal['loopback_stub'] = 'loopback_stub'
    synthetic_input_sha256: str = Field(pattern=DIGEST)
    local_provider_override: Literal[True] = True

    @model_validator(mode='after')
    def consistent(self):
        if self.request_observed and self.request_count == 0:
            raise ValueError('Observed request requires a request count')
        if any(field not in REQUEST_FIELDS for field in self.request_fields):
            raise ValueError('Request fields must be allowlisted')
        if len(self.request_fields) != len(set(self.request_fields)):
            raise ValueError('Repeated request fields')
        if (len(self.unknown_request_fields) != len(set(self.unknown_request_fields)) or
                any(not re.fullmatch(r'[a-z_]{1,64}', name) or name in REQUEST_FIELDS
                    for name in self.unknown_request_fields)):
            raise ValueError('Unknown request fields must be unique bounded API identifiers')
        if any(not re.fullmatch(r'[A-Z_]{1,80}', code) for code in self.blockers):
            raise ValueError('Invalid blocker code')
        if self.tool_count is not None and self.tool_count != len(self.tools):
            raise ValueError('Tool count differs from observed tools')
        if self.tools and not self.request_observed:
            raise ValueError('Tools require an observed request')
        if self.schema_observed != (self.schema_digest is not None):
            raise ValueError('Schema identity does not match observation state')
        if self.schema_matches is not None and not self.schema_observed:
            raise ValueError('Schema match requires schema observation')
        if not self.request_observed and (self.tool_count is not None or self.schema_observed or self.request_fields or self.unknown_request_fields):
            raise ValueError('Request metadata requires observation')
        if self.client_metadata_shape is not None:
            if not self.request_observed or 'client_metadata' not in self.request_fields:
                raise ValueError('Client metadata shape requires an observed envelope')
            if self.client_metadata_shape.object_observed != (self.client_metadata_shape.entry_count is not None):
                raise ValueError('Client metadata shape and count disagree')
        if not self.process_stopped and self.status != 'unknown':
            raise ValueError('Unconfirmed cleanup must remain unknown')
        return self


def validate_observation(value: dict) -> dict:
    return LocalCliObservation.model_validate(value).model_dump(mode='json')


class _Recorder:
    def __init__(self, config: ProviderConfig, schema: dict, path: str):
        self.config, self.schema, self.path = config, schema, path
        self.request_count = 0
        self.observed = False
        self.fields = []
        self.unknown_fields = []
        self.client_metadata_shape = None
        self.model = None
        self.tools = []
        self.tool_count = None
        self.schema_digest = None
        self.schema_matches = None
        self.blockers = set()
        self.tasks = set()
        self.cancel = asyncio.Event()

    def observe(self, body: bytes):
        value = strict_json(body, max_bytes=MAX_REQUEST_BYTES, max_depth=64)
        if not isinstance(value, dict):
            raise ValueError('request must be an object')
        self.observed = True
        self.fields = sorted(set(value) & REQUEST_FIELDS)
        unknown_fields = set(value) - REQUEST_FIELDS
        self.unknown_fields = sorted(name for name in unknown_fields if re.fullmatch(r'[a-z_]{1,64}', name))[:40]
        if unknown_fields:
            self.blockers.add('UNKNOWN_REQUEST_FIELDS')
        if 'client_metadata' in value:
            metadata = value['client_metadata']
            bounded_object = isinstance(metadata, dict) and len(metadata) <= 32
            self.client_metadata_shape = {
                'object_observed': bounded_object,
                'entry_count': len(metadata) if bounded_object else None,
                'values_recorded': False,
            }
            if not bounded_object:
                self.blockers.add('CLIENT_METADATA_SHAPE_INVALID')
        self.model = value.get('model') if value.get('model') == self.config.model else None
        if self.model is None:
            self.blockers.add('REQUEST_MODEL_MISMATCH')
        tools = value.get('tools')
        if not isinstance(tools, list) or len(tools) > 512:
            self.blockers.add('TOOLS_UNOBSERVABLE')
        else:
            clean = []
            for item in tools:
                if not isinstance(item, dict):
                    self.blockers.add('TOOLS_UNOBSERVABLE')
                    break
                name = item.get('name')
                if name is None and isinstance(item.get('function'), dict):
                    name = item['function'].get('name')
                try:
                    clean.append(ObservedTool(type=item.get('type'), name=name).model_dump(mode='json'))
                except ValueError:
                    self.blockers.add('TOOLS_UNOBSERVABLE')
                    break
            else:
                self.tools, self.tool_count = clean, len(clean)
                if clean:
                    self.blockers.add('TOOLS_EXPOSED')
        text = value.get('text')
        format_value = text.get('format') if isinstance(text, dict) else None
        wire = format_value.get('schema') if isinstance(format_value, dict) else None
        if isinstance(wire, dict):
            self.schema_digest = digest(wire)
            self.schema_matches = self.schema_digest == digest(self.schema)
            if not self.schema_matches:
                self.blockers.add('WIRE_SCHEMA_MISMATCH')
        else:
            self.blockers.add('SCHEMA_UNOBSERVABLE')

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        current = asyncio.current_task()
        self.tasks.add(current)
        response_status = '400 Bad Request'
        try:
            self.request_count = min(4, self.request_count + 1)
            if self.request_count > 3 or len(self.tasks) > 3:
                self.blockers.add('REQUEST_LIMIT')
                self.cancel.set()
                response_status = '429 Too Many Requests'
                return
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=2)
            if len(header) > 16384:
                raise ValueError('header limit')
            # Header bytes remain ephemeral: no logging, exception interpolation,
            # request dump, or report field includes them.
            lines = header.decode('latin-1').split('\r\n')
            request_line = lines[0].split(' ')
            if len(request_line) != 3 or request_line[:2] != ['POST', self.path]:
                self.blockers.add('UNEXPECTED_HTTP_REQUEST')
                response_status = '404 Not Found'
                return
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                key, value = line.split(':', 1)
                key = key.strip().lower()
                if key in headers:
                    raise ValueError('duplicate header')
                headers[key] = value.strip()
            if 'authorization' in headers or 'proxy-authorization' in headers:
                self.blockers.add('AUTHORIZATION_HEADER_PRESENT')
            if headers.get('content-encoding', 'identity') != 'identity' or 'transfer-encoding' in headers:
                self.blockers.add('UNSUPPORTED_HTTP_ENCODING')
                return
            length = int(headers.get('content-length', '-1'))
            if not 0 <= length <= MAX_REQUEST_BYTES:
                self.blockers.add('REQUEST_SIZE_LIMIT')
                response_status = '413 Content Too Large'
                return
            body = await asyncio.wait_for(reader.readexactly(length), timeout=2)
            try:
                self.observe(body)
            except (ValueError, UnicodeError, RecursionError):
                self.blockers.add('REQUEST_BODY_INVALID')
        except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            self.blockers.add('HTTP_PROTOCOL_ERROR')
        finally:
            # This stub has no forwarding implementation and never fabricates a
            # successful model response. A captured request receives a fixed 400.
            body = b'{"error":{"message":"Local inspection only; no model was invoked.","type":"invalid_request_error","code":"local_probe_complete"}}'
            try:
                writer.write(f'HTTP/1.1 {response_status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
                await asyncio.wait_for(writer.drain(), timeout=.5)
            except (OSError, TimeoutError, asyncio.CancelledError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.tasks.discard(current)


async def probe_local_cli(config: ProviderConfig, output_dir: Path, *, max_seconds=20,
                          contract_id=CONTRACT_ID) -> dict:
    """Return and save structural observations; never grant real dispatch."""
    from .provider_cli_profile import build_cli_argv, build_profile

    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 20:
        raise ValueError('Local probe requires a positive budget no greater than 20 seconds')
    output_dir = Path(output_dir).absolute()
    if '..' in output_dir.parts or any(parent.is_symlink() for parent in output_dir.parents):
        raise ValueError('Probe output cannot traverse symbolic links or parent traversal')
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError('Probe output directory must be new')
    profile = build_profile(config, contract_id=contract_id)
    if hasattr(profile, 'model_dump'):
        profile = profile.model_dump(mode='json')
    output_dir.mkdir(parents=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix='paper-alpha-local-cli-probe-')).resolve()
    cli_home = temporary / 'cli-state'
    cli_home.mkdir(mode=0o700)
    nonce = uuid.uuid4().hex
    endpoint_path = '/probe-' + nonce + '/v1/responses'
    recorder = _Recorder(config, profile['wire_schema'], endpoint_path)
    capture = _Capture()
    version_capture = _Capture()
    server = None
    deadline = time.monotonic() + max_seconds
    version = None
    transport_error = None
    resolved = profile['executable']['resolved']
    execution_config = config.model_copy(update={'executable': resolved or config.executable})
    try:
        if resolved is None:
            transport_error = 'EXECUTABLE_UNAVAILABLE'
        else:
            await _run_bounded([resolved, '--version'], cwd=temporary,
                deadline=deadline, cancel=recorder.cancel, output_limit=65536,
                supervision_dir=temporary / 'version-supervision', isolated_cli_home=cli_home,
                capture=version_capture)
        if not version_capture.stopped:
            capture.stopped = False
            transport_error = 'SUPERVISION_UNCONFIRMED'
        elif transport_error is None and (version_capture.error or version_capture.returncode != 0):
            transport_error = version_capture.error or 'CLI_VERSION_FAILED'
        elif transport_error is None:
            version_text = version_capture.stdout.decode('utf-8', errors='replace').strip()
            if re.fullmatch(r'codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:[-+][\w.-]+)?', version_text):
                version = version_text.removeprefix('codex-cli ')
            if version != config.expected_cli_version:
                transport_error = 'CLI_VERSION_MISMATCH'
        if transport_error is None:
            try:
                server = await asyncio.start_server(recorder.handle, '127.0.0.1', 0, limit=16384)
            except OSError:
                transport_error = 'LOOPBACK_BIND_FAILED'
            if server is not None:
                port = server.sockets[0].getsockname()[1]
                schema_path = temporary / 'output.schema.json'
                schema_path.write_text(json.dumps(profile['wire_schema'], ensure_ascii=False))
                argv = build_cli_argv(execution_config, temporary, schema_path, temporary / 'response.raw.json')
                argv += ['-c', 'model_provider="paper_alpha_probe"',
                    '-c', 'model_providers.paper_alpha_probe.name="Local inspection only"',
                    '-c', f'model_providers.paper_alpha_probe.base_url="http://127.0.0.1:{port}/probe-{nonce}/v1"',
                    '-c', 'model_providers.paper_alpha_probe.wire_api="responses"',
                    '-c', 'model_providers.paper_alpha_probe.requires_openai_auth=false',
                    '-c', 'model_providers.paper_alpha_probe.request_max_retries=0',
                    '-c', 'model_providers.paper_alpha_probe.stream_max_retries=0',
                    '-c', 'features.enable_request_compression=false']
                await _run_bounded(argv, cwd=temporary, stdin=SYNTHETIC_PROMPT.encode(),
                    deadline=deadline, cancel=recorder.cancel, output_limit=262144,
                    supervision_dir=temporary / 'request-supervision', capture=capture,
                    response_path=temporary / 'response.raw.json', response_limit=65536,
                    isolated_cli_home=cli_home)
                transport_error = capture.error
                if capture.returncode != 0 and transport_error is None:
                    transport_error = 'LOCAL_STUB_REJECTED' if recorder.observed else 'CLI_REQUEST_FAILED'
    except asyncio.CancelledError:
        transport_error = 'PROBE_INTERRUPTED'
        recorder.cancel.set()
        raise
    except (OSError, ValueError):
        transport_error = 'PROBE_FAILED'
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        for task in list(recorder.tasks):
            task.cancel()
        if recorder.tasks:
            await asyncio.gather(*list(recorder.tasks), return_exceptions=True)
        capture.stopped = capture.stopped and version_capture.stopped
        blockers = set(recorder.blockers) | {'REMOTE_BACKEND_UNVERIFIED', 'PRODUCTION_BOUNDARY_UNVERIFIED'}
        current_profile = build_profile(config, contract_id=contract_id)
        if hasattr(current_profile, 'model_dump'):
            current_profile = current_profile.model_dump(mode='json')
        if current_profile['profile_digest'] != profile['profile_digest']:
            blockers.add('PROFILE_DRIFT')
        if not recorder.observed:
            blockers.add('REQUEST_NOT_OBSERVED')
        if not capture.stopped:
            blockers.add('CLEANUP_UNCONFIRMED')
        status = 'unknown' if not capture.stopped else ('blocked' if recorder.observed else 'failed')
        observation = LocalCliObservation(
            evidence_kind='fixture' if config.mode == 'fixture' else 'local_cli_probe',
            profile_digest=profile.get('profile_digest', digest(profile)), status=status,
            cli_version=version, request_count=recorder.request_count,
            request_observed=recorder.observed, request_fields=recorder.fields,
            unknown_request_fields=recorder.unknown_fields,
            client_metadata_shape=recorder.client_metadata_shape,
            requested_model=recorder.model, tools=recorder.tools, tool_count=recorder.tool_count,
            schema_observed=recorder.schema_digest is not None, schema_digest=recorder.schema_digest,
            schema_matches=recorder.schema_matches, process_stopped=capture.stopped,
            transport_error=transport_error, blockers=sorted(blockers),
            synthetic_input_sha256=hashlib.sha256(SYNTHETIC_PROMPT.encode()).hexdigest())
        _atomic_bytes(output_dir / 'report.json', (observation.model_dump_json(indent=2) + '\n').encode())
        _atomic_bytes(output_dir / 'schema.json', (json.dumps(profile['wire_schema'], ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode())
        # Only fixed supervisor metadata is public, never its argv/stdin/logs.
        for source in ('version-supervision', 'request-supervision'):
            path = temporary / source / 'stop.json'
            try:
                stop = strict_json(_bounded_private_file(path, 65536), max_bytes=65536)
                allowed = {'protocol_version', 'nonce', 'invocation_started', 'reason', 'leader_reaped',
                           'group_absent', 'process_stopped', 'exit_code', 'stdout_bytes', 'stderr_bytes',
                           'stdout_sha256', 'stderr_sha256', 'response_saved', 'response_sha256'}
                public_stop = {key: value for key, value in stop.items() if key in allowed}
                _atomic_bytes(output_dir / (source + '.json'), (json.dumps(public_stop, sort_keys=True) + '\n').encode())
            except (OSError, ValueError):
                pass
        if capture.stopped:
            shutil.rmtree(temporary)
    return observation.model_dump(mode='json')
