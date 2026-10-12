"""Bounded Codex CLI transport; activation and business acceptance stay outside it.

The CLI is a harness, not a single model request. A subprocess exit or a valid
JSON document is never proof that a research draft is domain-valid.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from typing import Any
import uuid

from .provider_schema import (
    PreparedProposalRequest, ProviderAttemptResult, ProviderCallControl,
    ProviderConfig, ProviderError, ProviderReadiness, ProviderReceipt, ProviderUsage,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Capture:
    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int | None = None
    error: str | None = None
    started: bool = False
    stopped: bool = True
    protocol_completed: bool = False
    app_server_summary: dict | None = None


def _safe_environment() -> dict[str, str]:
    # Authentication remains in the CLI's own store. Do not forward application
    # secrets, API key overrides, shell startup settings or arbitrary client env.
    names = {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME",
             "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {name: value for name, value in os.environ.items() if name in names}


def _bounded_private_file(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Invalid supervision file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Supervision file exceeds limit")
    return data


def _read_stop(root: Path, nonce: str, supervisor_pid: int, deadline: float,
               output_limit: int, *, protocol_version: int = 1) -> tuple[dict, bytes, bytes]:
    if type(protocol_version) is not int:
        raise ValueError("Invalid supervision protocol version")
    stop = _strict_json(_bounded_private_file(root / "stop.json", 65536))
    required = {"protocol_version", "nonce", "supervisor_pid", "cli_pid", "invocation_started",
                "started_monotonic", "finished_monotonic", "deadline_monotonic", "exit_code",
                "reason", "leader_reaped", "group_absent", "process_stopped", "stdout_bytes",
                "stderr_bytes", "stdout_sha256", "stderr_sha256", "response_saved",
                "response_error", "response_sha256"}
    if protocol_version == 2:
        required |= {"transport", "rpc_completed", "rpc_result_sha256"}
    elif protocol_version != 1:
        raise ValueError("Unsupported supervision protocol")
    if not isinstance(stop, dict) or set(stop) != required:
        raise ValueError("Incomplete supervision confirmation")
    if (type(stop["protocol_version"]) is not int or stop["protocol_version"] != protocol_version or stop["nonce"] != nonce or
            stop["supervisor_pid"] != supervisor_pid or stop["deadline_monotonic"] != deadline):
        raise ValueError("Supervision identity mismatch")
    for key in ("invocation_started", "leader_reaped", "group_absent", "process_stopped", "response_saved"):
        if type(stop[key]) is not bool:
            raise ValueError("Invalid supervision state")
    if stop["process_stopped"] != (stop["leader_reaped"] and stop["group_absent"]):
        raise ValueError("Inconsistent supervision confirmation")
    for key in ("started_monotonic", "finished_monotonic", "deadline_monotonic"):
        if type(stop[key]) not in (int, float) or not math.isfinite(stop[key]):
            raise ValueError("Invalid supervision time")
    if stop["finished_monotonic"] < stop["started_monotonic"]:
        raise ValueError("Invalid supervision time ordering")
    if stop["invocation_started"]:
        if type(stop["cli_pid"]) is not int or stop["cli_pid"] <= 0:
            raise ValueError("Missing owned child identity")
    elif stop["cli_pid"] is not None:
        raise ValueError("Unexpected child identity")
    if stop["exit_code"] is not None and type(stop["exit_code"]) is not int:
        raise ValueError("Invalid owned child exit status")
    for key in ("reason", "response_error"):
        if stop[key] is not None and (not isinstance(stop[key], str) or not re.fullmatch(r"[A-Z_]{1,80}", stop[key])):
            raise ValueError("Invalid supervision error code")
    for key in ("stdout_bytes", "stderr_bytes"):
        if type(stop[key]) is not int or not 0 <= stop[key] <= output_limit:
            raise ValueError("Invalid supervision byte count")
    stdout = _bounded_private_file(root / "stdout.bin", output_limit)
    stderr = _bounded_private_file(root / "stderr.bin", output_limit)
    if len(stdout) + len(stderr) > output_limit:
        raise ValueError("Supervision output exceeded budget")
    for label, data in (("stdout", stdout), ("stderr", stderr)):
        if len(data) != stop[label + "_bytes"] or hashlib.sha256(data).hexdigest() != stop[label + "_sha256"]:
            raise ValueError("Supervision output digest mismatch")
    if stop["response_saved"]:
        response = _bounded_private_file(root / "response.raw.json", 65536)
        if hashlib.sha256(response).hexdigest() != stop["response_sha256"]:
            raise ValueError("Supervision response digest mismatch")
    elif stop["response_sha256"] is not None:
        raise ValueError("Unexpected supervision response digest")
    if protocol_version == 2:
        if stop["transport"] != "app_server" or type(stop["rpc_completed"]) is not bool:
            raise ValueError("Invalid App Server supervision state")
        summary_data = _bounded_private_file(root / "rpc-result.json", 65536)
        if hashlib.sha256(summary_data).hexdigest() != stop["rpc_result_sha256"]:
            raise ValueError("App Server summary digest mismatch")
        summary = _strict_json(summary_data)
        if (not isinstance(summary, dict) or type(summary.get("completed")) is not bool or
                summary["completed"] != stop["rpc_completed"]):
            raise ValueError("Invalid App Server completion confirmation")
        if stop["response_saved"] and (not stop["rpc_completed"] or stop["reason"] is not None or
                                       stop["exit_code"] != 0 or not stop["process_stopped"]):
            raise ValueError("App Server response lacks completed owned execution")
    return stop, stdout, stderr


async def _run_bounded(
    argv: list[str], *, cwd: Path, stdin: bytes = b"", deadline: float,
    cancel: asyncio.Event, output_limit: int, pass_fds: tuple[int, ...] = (),
    response_path: Path | None = None, response_limit: int = 65536,
    capture: _Capture | None = None, supervision_dir: Path | None = None,
    isolated_cli_home: Path | None = None,
    app_server_plan: dict | None = None,
) -> _Capture:
    """Supervise one child without relying on this worker to remain alive."""
    environment = _safe_environment()
    if isolated_cli_home is not None:
        # Host-only diagnostic isolation. This changes only the child CLI's
        # dedicated state location, never the user's environment or config.
        state_path = Path(isolated_cli_home)
        if (not state_path.is_absolute() or not state_path.is_dir()
                or any(part.is_symlink() for part in (state_path, *state_path.parents))):
            raise ValueError("Isolated CLI state must be an absolute unlinked directory")
        environment['CODEX_HOME'] = str(state_path)
        if app_server_plan is not None:
            # V2 app-server ignores exec's ignore-user-config loader override.
            # A dedicated HOME also prevents discovery of user-home skills and
            # project-independent config; legacy exec environment stays intact.
            environment['HOME'] = str(state_path)
    result = capture if capture is not None else _Capture()
    if cancel.is_set():
        result.error = "CANCELLED"
        return result
    if time.monotonic() >= deadline:
        result.error = "TIMEOUT"
        return result
    protocol_version = 1
    if app_server_plan is not None:
        if stdin or response_path is None:
            raise ValueError("App Server requires a closed plan and response path")
        from .provider_app_server_protocol import AppServerProtocol
        AppServerProtocol(app_server_plan, response_limit=response_limit)
        if app_server_plan.get("cwd") != str(cwd):
            raise ValueError("App Server plan differs from the owned working directory")
        stdin = json.dumps(app_server_plan, ensure_ascii=False, allow_nan=False,
                           sort_keys=True, separators=(",", ":")).encode()
        if len(stdin) > 131072:
            raise ValueError("App Server input plan exceeds byte limit")
        protocol_version = 2
    nonce = uuid.uuid4().hex
    root = supervision_dir or (cwd / (".supervision-" + nonce))
    root.mkdir(mode=0o700)
    _atomic_bytes(root / "stdin.bin", stdin)
    spec = {
        "protocol_version": protocol_version, "nonce": nonce, "argv": argv, "cwd": str(cwd),
        "deadline_monotonic": deadline, "output_limit": output_limit,
        "response_path": str(response_path) if response_path else None,
        "response_limit": response_limit, "pass_fds": list(pass_fds),
    }
    if protocol_version == 2:
        spec["transport"] = "app_server"
    _atomic_bytes(root / "spec.json", _json_bytes(spec))
    from .provider_supervision_journal import ACTIVE_JOURNAL
    journal = ACTIVE_JOURNAL.get()
    journal_options = {"protocol_version": 2, "transport": "app_server"} if protocol_version == 2 else {}
    journal_stage = journal.prepare(nonce=nonce, directory=root, deadline=deadline,
        output_limit=output_limit, phase='version' if argv[1:] == ['--version'] else
        'auth' if argv[1:] == ['login', 'status'] else 'app_server' if protocol_version == 2 else 'cli',
        **journal_options) if journal is not None else None
    parent_read, parent_write = os.pipe()
    process = None
    diagnostic_task = None
    interrupted = False
    requested_stop = None

    async def diagnostics(stream):
        data = bytearray()
        while chunk := await stream.read(4096):
            data.extend(chunk[:max(0, 65536 - len(data))])
        return bytes(data)

    def request_stop():
        nonlocal parent_write
        if parent_write is not None:
            try:
                os.write(parent_write, b"C")
            except OSError:
                pass
            os.close(parent_write)
            parent_write = None

    async def await_supervisor():
        # Do not kill an unresponsive supervisor: it may still own a live child
        # and inherited lease. Unknown cleanup must remain fenced for recovery.
        try:
            await asyncio.wait_for(process.wait(), timeout=4.5)
        except TimeoutError:
            result.error = "SUPERVISION_UNCONFIRMED"

    try:
        try:
            result.stopped = False
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(Path(__file__).with_name("provider_supervisor.py")),
                "--spec", str(root / "spec.json"), "--parent-fd", str(parent_read),
                cwd=cwd, env=environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, pass_fds=(parent_read, *pass_fds),
            )
        except OSError:
            result.error = "SUPERVISOR_UNAVAILABLE"
            result.stopped = True
            return result
        finally:
            os.close(parent_read)
            parent_read = None
        diagnostic_task = asyncio.create_task(diagnostics(process.stderr))
        if journal is not None:
            try:
                journal.started(journal_stage, process.pid)
            except (OSError, ValueError):
                request_stop()
                await asyncio.shield(await_supervisor())
                raise
        try:
            while process.returncode is None:
                if cancel.is_set():
                    requested_stop = "CANCELLED"
                    request_stop()
                    break
                if time.monotonic() >= deadline:
                    requested_stop = "TIMEOUT"
                    request_stop()
                    break
                await asyncio.sleep(.01)
        except asyncio.CancelledError:
            interrupted = True
            requested_stop = "INTERRUPTED"
            request_stop()
        await asyncio.shield(await_supervisor())
    finally:
        if parent_read is not None:
            os.close(parent_read)
        if parent_write is not None:
            os.close(parent_write)
        if process is not None:
            try:
                stop, result.stdout, result.stderr = _read_stop(root, nonce, process.pid, deadline, output_limit,
                                                              protocol_version=protocol_version)
                result.started = stop["invocation_started"]
                result.returncode = stop["exit_code"]
                result.stopped = bool(stop["process_stopped"] and process.returncode == 0)
                result.error = requested_stop or stop["reason"]
                if protocol_version == 2:
                    result.protocol_completed = stop["rpc_completed"]
                    result.app_server_summary = _strict_json(_bounded_private_file(root / "rpc-result.json", 65536))
                    result.error = result.error or stop["response_error"]
                    if result.error is None and not result.protocol_completed:
                        result.error = "APP_SERVER_INCOMPLETE"
                    if result.error is None and result.returncode != 0:
                        result.error = "APP_SERVER_EXIT_NONZERO"
                    if result.error is None and not stop["response_saved"]:
                        result.error = "APP_SERVER_RESPONSE_MISSING"
                if not result.stopped:
                    result.error = "SUPERVISION_UNCONFIRMED"
            except (OSError, ValueError):
                result.stopped = False
                result.error = "SUPERVISION_UNCONFIRMED"
                try:
                    launched = _strict_json(_bounded_private_file(root / "launched.json", 4096))
                    result.started = launched.get("nonce") == nonce and launched.get("supervisor_pid") == process.pid
                except (OSError, ValueError):
                    pass
                for label in ("stdout", "stderr"):
                    try:
                        setattr(result, label, _bounded_private_file(root / (label + ".bin"), output_limit))
                    except (OSError, ValueError):
                        pass
            if diagnostic_task is not None:
                try:
                    diagnostic_bytes = await asyncio.wait_for(diagnostic_task, timeout=.2)
                    _atomic_bytes(root / "supervisor.stderr.log", diagnostic_bytes)
                except (TimeoutError, OSError):
                    diagnostic_task.cancel()
                    await asyncio.gather(diagnostic_task, return_exceptions=True)
        if journal is not None:
            try:
                journal.finish(journal_stage, stopped=result.stopped,
                               spawn_failed=process is None and result.error == 'SUPERVISOR_UNAVAILABLE')
            except (OSError, ValueError):
                result.error = 'SUPERVISION_JOURNAL_ERROR'
    if interrupted:
        raise asyncio.CancelledError
    return result


@contextmanager
def _invocation_directory(capture: _Capture, *, prefix="paper-alpha-provider-call-"):
    root = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield root
    finally:
        # An unconfirmed invocation may still use this directory. Retain it for
        # reconciliation instead of deleting files underneath a live process.
        if capture.stopped:
            shutil.rmtree(root)


def _atomic_bytes(path: Path, content: bytes) -> None:
    """Publish only into a host-owned, previously unused attempt directory."""
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Attempt artifact already exists: {path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication provides atomic no-replace semantics.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _strict_json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON constant")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number

    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                             parse_constant=constant, parse_float=finite_float)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the decoder limit") from exc
    # Some Python decoders accept nesting far beyond the Python recursion limit.
    # Bound the representation explicitly before downstream traversal/encoding.
    pending = [(decoded, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON nesting exceeds the event contract limit")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    return decoded


def _events(raw: bytes) -> dict[str, Any]:
    """Summarize observable events without inventing model-call or cost counts."""
    events = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        item = _strict_json(line)
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError("Invalid CLI event")
        events.append(item)
    usages = []
    starts = completions = tools = 0
    complete = True
    open_turn = False
    seen_item_ids: set[str] = set()
    for event in events:
        kind = event["type"]
        if kind == "turn.started":
            if open_turn:
                complete = False
            starts += 1
            open_turn = True
        elif kind == "turn.completed":
            if not open_turn:
                complete = False
                continue
            open_turn = False
            completions += 1
            usage = event.get("usage")
            if not isinstance(usage, dict):
                complete = False
            elif any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                     for value in usage.values()):
                complete = False
            else:
                usages.append(usage)
                if not {"input_tokens", "output_tokens"}.issubset(usage):
                    complete = False
        elif kind in {"turn.failed", "error"}:
            complete = False
        elif kind == "item.completed":
            item = event.get("item", {})
            if not isinstance(item, dict):
                raise ValueError("Invalid CLI item")
            if item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                item_id = item.get("id")
                if not isinstance(item_id, str) or item_id not in seen_item_ids:
                    tools += 1
                if isinstance(item_id, str):
                    seen_item_ids.add(item_id)
    return {"raw_events": events, "usage_events": usages,
            "usage_complete": bool(complete and starts and starts == completions and len(usages) == completions),
            "observed_turns": starts, "observed_tool_events": tools,
            "turn_failed": any(item["type"] in {"error", "turn.failed"} for item in events)}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()


def _usage(summary: dict[str, Any]) -> ProviderUsage:
    raw = summary["usage_events"]

    def total(field: str) -> int | None:
        values = [item[field] for item in raw if field in item]
        return sum(values) if values else None

    return ProviderUsage(
        input_tokens=total("input_tokens"), output_tokens=total("output_tokens"),
        cached_input_tokens=total("cached_input_tokens"),
        reasoning_tokens=total("reasoning_output_tokens"),
        complete=summary["usage_complete"], observed_turns=summary["observed_turns"],
        tool_events=summary["observed_tool_events"], raw=raw,
    )


class CodexCliProvider:
    """Trusted host configuration; real inference defaults to gated off.

    Fixture mode tests the local transport and is always marked in the receipt.
    It does not attest any real CLI model or isolation capability.
    """

    def __init__(self, config: ProviderConfig):
        self.config = config.model_copy(deep=True)

    async def check_ready(self) -> ProviderReadiness:
        return await self._check_ready()

    async def _check_ready(self, *, pass_fds: tuple[int, ...] = (),
                           cleanup: _Capture | None = None,
                           cancel_event: asyncio.Event | None = None) -> ProviderReadiness:
        # Cleanup evidence is internal: the frozen public readiness model is
        # informational, whereas a claimed request must preserve lease/unknown
        # state even when its readiness coroutine is cancelled or times out.
        cleanup = cleanup if cleanup is not None else _Capture()
        config = self.config
        result = ProviderReadiness(
            provider=config.mode, configured=bool(config.enabled and config.model),
            authenticated=None, inference_verified=False,
            tool_boundary_verified=False, checked_at=_now(),
        )
        if not config.enabled:
            result.errors.append(ProviderError(code="PROVIDER_DISABLED", message="Proposal provider is disabled."))
            return result
        if not config.model:
            result.errors.append(ProviderError(code="CONFIGURATION_ERROR", message="An explicit model is required."))
            return result
        with _invocation_directory(cleanup, prefix="paper-alpha-provider-probe-") as root:
            deadline = time.monotonic() + 5
            cancel = cancel_event if cancel_event is not None else asyncio.Event()

            async def probe(arguments):
                captured = _Capture()
                try:
                    return await _run_bounded([config.executable, *arguments], cwd=root,
                        deadline=deadline, cancel=cancel, output_limit=65536,
                        pass_fds=pass_fds, capture=captured)
                finally:
                    cleanup.stopped = cleanup.stopped and captured.stopped

            version = await probe(["--version"])
            if version.error or version.returncode != 0:
                result.errors.append(ProviderError(code=version.error or "CLI_UNAVAILABLE", message="CLI version probe failed."))
                return result
            match = re.fullmatch(r"codex-cli ([0-9]+\.[0-9]+\.[0-9]+(?:[-+][\w.-]+)?)\s*", version.stdout.decode("utf-8", errors="replace"))
            result.cli_version = match.group(1) if match else None
            if result.cli_version != config.expected_cli_version:
                result.errors.append(ProviderError(code="CLI_INCOMPATIBLE", message="CLI version differs from the frozen provider configuration."))
            help_result = await probe(["exec", "--help"])
            flags = (b"--json", b"--output-schema", b"--output-last-message", b"--sandbox",
                     b"--ephemeral", b"--ignore-user-config", b"--skip-git-repo-check", b"--cd")
            if help_result.error or help_result.returncode != 0 or any(flag not in help_result.stdout for flag in flags):
                result.errors.append(ProviderError(code=help_result.error or "CLI_INCOMPATIBLE", message="Required CLI exec options are unavailable."))
                if not help_result.stopped:
                    return result
            if config.mode == "codex_cli":
                auth = await probe(["login", "status"])
                if not auth.stopped:
                    result.errors.append(ProviderError(code="SUPERVISION_UNCONFIRMED", message="Authentication probe cleanup was not confirmed."))
                    return result
                # Classify without storing credential-adjacent probe output.
                status_text = (auth.stdout + auth.stderr).decode("utf-8", errors="replace").lower()
                result.authenticated = bool(not auth.error and auth.returncode == 0 and "chatgpt" in status_text and "logged in" in status_text)
                if not result.authenticated:
                    result.errors.append(ProviderError(code="AUTH_REQUIRED", message="Saved ChatGPT CLI authentication was not confirmed."))
                result.errors.append(ProviderError(code="COMPATIBILITY_UNVERIFIED", message="Real inference requires independently verified evidence and host dispatch authorization; configuration booleans do not authorize it."))
        return result

    async def propose(self, request: PreparedProposalRequest, control: ProviderCallControl) -> ProviderAttemptResult:
        started = time.monotonic()
        receipt = ProviderReceipt(proposal_id=request.proposal_id, attempt_id=request.attempt_id,
                                  started_at=_now(), finished_at=_now(), elapsed_seconds=0,
                                  requested_model=request.config.model, fixture=request.config.mode == "fixture")
        raw_response: bytes | None = None
        error: ProviderError | None = None
        captured = _Capture()
        artifacts = control.artifact_dir
        prepared = False

        def fail(code: str, message: str) -> ProviderAttemptResult:
            receipt.finished_at = _now()
            receipt.elapsed_seconds = max(0, time.monotonic() - started)
            return ProviderAttemptResult(None, ProviderError(code=code, message=message), receipt)

        if request.config != self.config:
            return fail("CONFIGURATION_ERROR", "Prepared configuration differs from the provider's frozen configuration.")
        if not self.config.enabled:
            return fail("PROVIDER_DISABLED", "Proposal provider is disabled.")
        if not self.config.model:
            return fail("CONFIGURATION_ERROR", "An explicit model is required.")
        if self.config.mode == "codex_cli":
            return fail("COMPATIBILITY_UNVERIFIED", "Real dispatch is closed until a verified host authorization path exists; self-declared configuration flags do not authorize inference.")
        if any(value is not None for value in (request.budget.hard_token_limit, request.budget.hard_model_call_limit, request.budget.hard_cost_limit)):
            return fail("UNSUPPORTED_BUDGET", "This transport has no verified supplier token, model-call or monetary hard cap.")
        input_bytes = request.input_text.encode("utf-8")
        if len(input_bytes) > request.budget.max_context_bytes:
            return fail("INPUT_LIMIT", "Prepared input exceeds the frozen byte limit.")
        if control.cancel_event.is_set():
            return fail("CANCELLED", "The invocation was cancelled before launch.")
        deadline = min(control.deadline_monotonic, started + request.budget.max_seconds)
        if time.monotonic() >= deadline:
            return fail("TIMEOUT", "The invocation deadline expired before launch.")
        probe_cleanup = _Capture()
        try:
            readiness = await asyncio.wait_for(
                self._check_ready(pass_fds=control.pass_fds, cleanup=probe_cleanup,
                                  cancel_event=control.cancel_event),
                timeout=max(.001, deadline - time.monotonic()))
        except TimeoutError:
            receipt.process_stopped = probe_cleanup.stopped
            if not probe_cleanup.stopped:
                return fail("SUPERVISION_UNCONFIRMED", "Readiness timed out and probe cleanup remains unconfirmed.")
            return fail("TIMEOUT", "CLI readiness checks exceeded the invocation deadline.")
        except asyncio.CancelledError:
            receipt.process_stopped = probe_cleanup.stopped
            if not probe_cleanup.stopped:
                return fail("SUPERVISION_UNCONFIRMED", "Readiness was interrupted and probe cleanup remains unconfirmed.")
            raise
        receipt.process_stopped = probe_cleanup.stopped
        if not probe_cleanup.stopped:
            return fail("SUPERVISION_UNCONFIRMED", "Readiness probe cleanup remains unconfirmed.")
        receipt.cli_version = readiness.cli_version
        if readiness.errors:
            return fail(readiness.errors[0].code, readiness.errors[0].message)
        try:
            if artifacts.is_symlink():
                return fail("ARTIFACT_CONFLICT", "The attempt artifact directory must not be a symbolic link.")
            artifacts.mkdir(parents=True, exist_ok=True)
            names = ("input.txt", "output.schema.json", "invocation.json", "stdout.jsonl", "stderr.log", "response.raw.json", "receipt.json", "supervision")
            if any((artifacts / name).exists() or (artifacts / name).is_symlink() for name in names):
                return fail("ARTIFACT_CONFLICT", "Attempt artifacts already exist; a retry requires a new attempt directory.")
            _atomic_bytes(artifacts / "input.txt", input_bytes)
            schema_bytes = _json_bytes(request.output_schema)
            _atomic_bytes(artifacts / "output.schema.json", schema_bytes)
            prepared = True
            with _invocation_directory(captured) as cwd:
                schema_path, response_path = cwd / "output.schema.json", cwd / "response.raw.json"
                schema_path.write_bytes(schema_bytes)
                argv = self._argv(cwd, schema_path, response_path)
                _atomic_bytes(artifacts / "invocation.json", _json_bytes({
                    "argv": argv, "config": self.config.model_dump(mode="json"),
                    "proposal_id": request.proposal_id, "attempt_id": request.attempt_id,
                    "context_digest": request.context_digest,
                    "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
                    "schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
                    "budget": request.budget.model_dump(mode="json"),
                }))
                captured = await _run_bounded(argv, cwd=cwd, stdin=input_bytes, deadline=deadline,
                    cancel=control.cancel_event, output_limit=request.budget.max_log_bytes,
                    pass_fds=control.pass_fds, response_path=response_path,
                    response_limit=request.budget.max_output_bytes, capture=captured,
                    supervision_dir=artifacts / "supervision")
                receipt.invocation_started = captured.started
                receipt.exit_code = captured.returncode
                if captured.error:
                    error = ProviderError(code=captured.error, message="CLI invocation stopped before a complete response was accepted.")
                elif captured.returncode != 0:
                    error = ProviderError(code="CLI_FAILED", message="CLI exited unsuccessfully; see retained diagnostics.")
                try:
                    summary = _events(captured.stdout)
                    receipt.usage = _usage(summary)
                    if error is not None:
                        receipt.usage.complete = False
                    elif summary["turn_failed"]:
                        error = ProviderError(code="CLI_FAILED", message="CLI reported a failed turn or error event.")
                    elif not any(item["type"] == "turn.completed" for item in summary["raw_events"]):
                        error = ProviderError(code="INCOMPLETE_EVENT_STREAM", message="CLI did not report a completed turn.")
                except (ValueError, UnicodeDecodeError):
                    if error is None:
                        error = ProviderError(code="INVALID_EVENT_STREAM", message="CLI stdout was not a valid JSONL event stream.")
                # Preserve bounded raw output on failure, but never accept it.
                try:
                    # Nonblocking open also prevents a substituted FIFO/device
                    # from hanging before fstat can reject the non-regular file.
                    fd = os.open(response_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        entry = os.fstat(stream.fileno())
                        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                            raise ValueError("Response is not a private regular file")
                        data = stream.read(request.budget.max_output_bytes + 1)
                    if len(data) > request.budget.max_output_bytes:
                        error = ProviderError(code="OUTPUT_LIMIT", message="CLI final response exceeds the frozen byte limit.")
                    else:
                        raw_response = data
                except FileNotFoundError:
                    if error is None:
                        error = ProviderError(code="MISSING_RESPONSE", message="CLI did not produce its final response file.")
                except (OSError, ValueError):
                    error = ProviderError(code="INVALID_RESPONSE_FILE", message="CLI final response is not a readable private regular file.")
                if error is None and (control.cancel_event.is_set() or time.monotonic() >= deadline):
                    error = ProviderError(code="CANCELLED" if control.cancel_event.is_set() else "TIMEOUT",
                                          message="Final response arrived after cancellation or the deadline.")
        except asyncio.CancelledError:
            error = ProviderError(code="INTERRUPTED", message="The caller cancelled the provider coroutine; process cleanup was attempted.")
            receipt.usage.complete = False
            raise
        except (OSError, ValueError, TypeError):
            error = ProviderError(code="ARTIFACT_ERROR", message="Provider could not prepare or persist its attempt artifacts.")
        finally:
            receipt.finished_at = _now()
            receipt.elapsed_seconds = max(0, time.monotonic() - started)
            receipt.invocation_started = captured.started
            receipt.exit_code = captured.returncode
            receipt.process_stopped = captured.stopped
            if error is not None and error.code in {"CANCELLED", "TIMEOUT", "INTERRUPTED", "PROCESS_CLEANUP_FAILED"}:
                receipt.usage.complete = False
            receipt.stdout_bytes, receipt.stderr_bytes = len(captured.stdout), len(captured.stderr)
            if raw_response is not None:
                receipt.response_sha256 = hashlib.sha256(raw_response).hexdigest()
            if prepared:
                try:
                    _atomic_bytes(artifacts / "stdout.jsonl", captured.stdout)
                    _atomic_bytes(artifacts / "stderr.log", captured.stderr)
                    if raw_response is not None:
                        _atomic_bytes(artifacts / "response.raw.json", raw_response)
                    _atomic_bytes(artifacts / "receipt.json", _json_bytes({
                        "receipt": receipt.model_dump(mode="json"),
                        "error": error.model_dump(mode="json") if error else None,
                    }))
                except OSError:
                    error = ProviderError(code="ARTIFACT_ERROR", message="Attempt artifacts could not be completely published.")
        return ProviderAttemptResult(raw_response, error, receipt)

    def _argv(self, cwd: Path, schema: Path, response: Path) -> list[str]:
        from .provider_cli_profile import build_cli_argv
        return build_cli_argv(self.config, cwd, schema, response)
