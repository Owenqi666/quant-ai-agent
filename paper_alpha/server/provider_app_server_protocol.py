"""Bounded, single-turn Codex 0.145.0 App Server protocol state machine.

This module owns no process, transport, authentication, clock, or retry. The
existing supervisor owns those resources and feeds complete stdout frames here.
The wire names follow the official rust-v0.145.0 app-server protocol. Final
domain/schema validation remains the original provider's responsibility.
"""
from __future__ import annotations

import copy
import json
from pathlib import PurePosixPath
from typing import Any

PROTOCOL_VERSION = 1
MAX_PLAN_BYTES = 131072
MAX_FRAME_BYTES = 1048576
MAX_TOTAL_BYTES = 8388608
MAX_EVENTS = 4096
MAX_EARLY_EVENTS = 64
MAX_ITEMS = 256
MAX_JSON_DEPTH = 64
MAX_RESPONSE_BYTES = 65536

_PLAN_KEYS = {
    "protocol_version", "cwd", "model", "effort", "prompt", "output_schema",
    "allow_file_changes",
}
_TOKEN_KEYS = {
    "cachedInputTokens", "cacheWriteInputTokens", "inputTokens", "outputTokens",
    "reasoningOutputTokens", "totalTokens",
}
_TURN_PROGRESS = {
    "item/agentMessage/delta", "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded", "item/reasoning/textDelta",
    "turn/plan/updated", "item/fileChange/outputDelta",
    "turn/diff/updated", "item/fileChange/patchUpdated",
}
_THREAD_PROGRESS = {"thread/status/changed", "thread/settings/updated"}
_GLOBAL_NOOP = {"account/rateLimits/updated"}
_EVENTS = (
    _TURN_PROGRESS | _THREAD_PROGRESS | _GLOBAL_NOOP
    | {"thread/started", "thread/closed", "turn/started", "turn/completed", "item/started",
       "item/completed", "thread/tokenUsage/updated", "remoteControl/status/changed", "error"}
)


class AppServerProtocolError(Exception):
    """Public stable code; never includes raw prompt or model output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AppServerProtocolError("APP_SERVER_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    raise AppServerProtocolError("APP_SERVER_NONFINITE_JSON")


def _depth(value: Any) -> None:
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise AppServerProtocolError("APP_SERVER_JSON_DEPTH_LIMIT")
        if isinstance(node, dict):
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)


def _encode(value: Any) -> bytes:
    try:
        _depth(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AppServerProtocolError("APP_SERVER_INVALID_JSON") from exc


def _decode(data: bytes) -> Any:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_nonfinite)
        _encode(value)  # Also rejects overflow-to-infinity and lone surrogates.
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AppServerProtocolError("APP_SERVER_INVALID_JSON") from exc


def _identifier(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 256
            and value.isascii() and all(32 < ord(char) < 127 for char in value))


class AppServerProtocol:
    """Exactly initialize -> one ephemeral thread -> one externally isolated turn."""

    def __init__(self, plan: dict, response_limit: int = MAX_RESPONSE_BYTES):
        if (not isinstance(plan, dict) or set(plan) != _PLAN_KEYS
                or type(plan["protocol_version"]) is not int
                or plan["protocol_version"] != PROTOCOL_VERSION
                or plan["model"] != "gpt-5.5" or plan["effort"] != "low"
                or type(plan["allow_file_changes"]) is not bool
                or not isinstance(plan["prompt"], str) or not plan["prompt"]
                or "\0" in plan["prompt"]
                or not isinstance(plan["output_schema"], dict)
                or not plan["output_schema"]):
            raise AppServerProtocolError("APP_SERVER_INVALID_PLAN")
        cwd = plan["cwd"]
        if (not isinstance(cwd, str) or "\0" in cwd
                or not PurePosixPath(cwd).is_absolute()
                or str(PurePosixPath(cwd)) != cwd or ".." in PurePosixPath(cwd).parts):
            raise AppServerProtocolError("APP_SERVER_INVALID_PLAN")
        if type(response_limit) is not int or not 1 <= response_limit <= MAX_RESPONSE_BYTES:
            raise AppServerProtocolError("APP_SERVER_INVALID_PLAN")
        if len(_encode(plan)) > MAX_PLAN_BYTES:
            raise AppServerProtocolError("APP_SERVER_PLAN_LIMIT")
        self._plan = copy.deepcopy(plan)
        self._response_limit = response_limit
        self.complete = False
        self.response: bytes | None = None
        self._state = "created"
        self._pending_id: int | None = None
        self._responded: set[int] = set()
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._turn_status: str | None = None
        self._usage: dict | None = None
        self._error_code: str | None = None
        self._frames = 0
        self._bytes = 0
        self._early: list[dict] = []
        self._items: dict[str, dict] = {}
        self._started_items: set[str] = set()
        self._completed_items: set[str] = set()
        self._thread_started_seen = False
        self._turn_started_seen = False

    def _fail(self, code: str) -> None:
        self._state = "failed"
        self._error_code = code
        self.complete = False
        self.response = None
        self._early.clear()
        raise AppServerProtocolError(code)

    @staticmethod
    def _frame(value: dict) -> bytes:
        return _encode(value) + b"\n"

    def start(self) -> list[bytes]:
        if self._state != "created":
            self._fail("APP_SERVER_INVALID_STATE")
        self._state = "initialize"
        self._pending_id = 1
        return [self._frame({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "paper_alpha", "title": "Paper-to-Alpha",
                           "version": "1"},
            "capabilities": {"experimentalApi": False},
        }})]

    def feed(self, line: bytes) -> list[bytes]:
        try:
            return self._feed(line)
        except AppServerProtocolError as exc:
            self._fail(exc.code)

    def _feed(self, line: bytes) -> list[bytes]:
        if self._state in {"created", "failed"}:
            self._fail(self._error_code or "APP_SERVER_INVALID_STATE")
        if not isinstance(line, bytes) or not line.endswith(b"\n") or line.count(b"\n") != 1:
            self._fail("APP_SERVER_INVALID_FRAME")
        self._frames += 1
        self._bytes += len(line)
        if len(line) > MAX_FRAME_BYTES:
            self._fail("APP_SERVER_FRAME_LIMIT")
        if self._bytes > MAX_TOTAL_BYTES or self._frames > MAX_EVENTS:
            self._fail("APP_SERVER_STREAM_LIMIT")
        message = _decode(line)
        if not isinstance(message, dict) or not message:
            self._fail("APP_SERVER_INVALID_MESSAGE")
        if message.get("jsonrpc", "2.0") != "2.0":
            self._fail("APP_SERVER_INVALID_MESSAGE")
        if "method" in message:
            if "id" in message:
                # A host approval/tool callback cannot be implicitly authorized.
                self._fail("APP_SERVER_APPROVAL_UNEXPECTED")
            if set(message) - {"method", "params", "jsonrpc", "emittedAtMs"}:
                self._fail("APP_SERVER_INVALID_MESSAGE")
            emitted_at = message.get("emittedAtMs")
            if emitted_at is not None and (type(emitted_at) is not int
                    or not 0 <= emitted_at <= 9223372036854775807):
                self._fail("APP_SERVER_INVALID_MESSAGE")
            method, params = message["method"], message.get("params")
            if not isinstance(method, str) or method not in _EVENTS:
                self._fail("APP_SERVER_UNEXPECTED_NOTIFICATION")
            if not isinstance(params, dict):
                self._fail("APP_SERVER_INVALID_MESSAGE")
            self._notification(message)
            return []
        return self._rpc_response(message)

    def _rpc_response(self, message: dict) -> list[bytes]:
        request_id = message.get("id")
        if type(request_id) is int and request_id in self._responded:
            self._fail("APP_SERVER_DUPLICATE_RPC_RESPONSE")
        if type(request_id) is not int or request_id != self._pending_id:
            self._fail("APP_SERVER_RPC_ID_MISMATCH")
        if set(message) - {"id", "result", "error", "jsonrpc"}:
            self._fail("APP_SERVER_INVALID_MESSAGE")
        if ("result" in message) == ("error" in message):
            self._fail("APP_SERVER_INVALID_MESSAGE")
        if "error" in message:
            self._fail("APP_SERVER_RPC_ERROR")
        result = message["result"]
        if not isinstance(result, dict):
            self._fail("APP_SERVER_INVALID_MESSAGE")
        self._responded.add(request_id)
        if request_id == 1:
            if not isinstance(result.get("userAgent"), str) or not result["userAgent"]:
                self._fail("APP_SERVER_INVALID_MESSAGE")
            self._state, self._pending_id = "thread_start", 2
            return [self._frame({"method": "initialized"}), self._frame({
                "id": 2, "method": "thread/start", "params": {
                    "cwd": self._plan["cwd"], "model": self._plan["model"],
                    "approvalPolicy": "never", "ephemeral": True,
                },
            })]
        if request_id == 2:
            thread = result.get("thread")
            if not isinstance(thread, dict) or not _identifier(thread.get("id")):
                self._fail("APP_SERVER_INVALID_MESSAGE")
            for key, expected in (("cwd", self._plan["cwd"]),
                                  ("model", self._plan["model"]),
                                  ("approvalPolicy", "never")):
                if key in result and result[key] != expected:
                    self._fail("APP_SERVER_CONFIGURATION_MISMATCH")
            self._validate_thread(thread)
            self._thread_id = thread["id"]
            self._state, self._pending_id = "turn_start", 3
            self._drain_early()
            return [self._frame({"id": 3, "method": "turn/start", "params": {
                "threadId": self._thread_id,
                "cwd": self._plan["cwd"], "model": self._plan["model"],
                "effort": self._plan["effort"], "approvalPolicy": "never",
                "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"},
                "outputSchema": self._plan["output_schema"],
                "input": [{"type": "text", "text": self._plan["prompt"]}],
            }})]
        if request_id == 3:
            turn = self._validate_turn(result.get("turn"), terminal=False)
            self._turn_id = turn["id"]
            self._state, self._pending_id = "running", None
            # RPC snapshots are not trusted as final completion notifications.
            self._drain_early()
            return []
        self._fail("APP_SERVER_RPC_ID_MISMATCH")

    def _validate_thread(self, thread: dict) -> None:
        if "ephemeral" in thread and thread["ephemeral"] is not True:
            self._fail("APP_SERVER_CONFIGURATION_MISMATCH")
        if "cwd" in thread and thread["cwd"] != self._plan["cwd"]:
            self._fail("APP_SERVER_CONFIGURATION_MISMATCH")
        if thread.get("turns", []) != []:
            self._fail("APP_SERVER_UNEXPECTED_HISTORY")

    def _buffer(self, message: dict) -> None:
        if len(self._early) >= MAX_EARLY_EVENTS:
            self._fail("APP_SERVER_EARLY_EVENT_LIMIT")
        self._early.append(message)

    def _drain_early(self) -> None:
        early, self._early = self._early, []
        for message in early:
            self._notification(message)

    def _scope(self, params: dict, *, turn: bool) -> None:
        if params.get("threadId") != self._thread_id:
            self._fail("APP_SERVER_THREAD_MISMATCH")
        if turn and params.get("turnId") != self._turn_id:
            self._fail("APP_SERVER_TURN_MISMATCH")

    def _notification(self, message: dict) -> None:
        method, params = message["method"], message["params"]
        if method == "error":
            self._fail("APP_SERVER_REMOTE_ERROR")
        if method == "remoteControl/status/changed":
            # This stable, global startup notification does not authorize any
            # remote-control capability. Only the disabled state is admissible.
            if set(params) != {"status", "serverName", "installationId", "environmentId"}:
                self._fail("APP_SERVER_INVALID_MESSAGE")
            if params["status"] != "disabled":
                self._fail("APP_SERVER_CONFIGURATION_MISMATCH")
            name, environment = params["serverName"], params["environmentId"]
            if (not isinstance(name, str) or len(name.encode("utf-8")) > 1024
                    or "\0" in name or not _identifier(params["installationId"])
                    or (environment is not None and not _identifier(environment))):
                self._fail("APP_SERVER_INVALID_MESSAGE")
            return
        if method in _GLOBAL_NOOP:
            return
        if self._state == "initialize":
            self._fail("APP_SERVER_UNEXPECTED_NOTIFICATION")
        if self._state == "thread_start":
            if method not in {"thread/started"} | _THREAD_PROGRESS:
                self._fail("APP_SERVER_UNEXPECTED_NOTIFICATION")
            self._buffer(message)
            return
        if method == "thread/started":
            thread = params.get("thread")
            if (self.complete or self._thread_started_seen or not isinstance(thread, dict)
                    or thread.get("id") != self._thread_id):
                self._fail("APP_SERVER_THREAD_MISMATCH")
            self._validate_thread(thread)
            self._thread_started_seen = True
            return
        if method in _THREAD_PROGRESS:
            self._scope(params, turn=False)
            if method == "thread/status/changed":
                status = params.get("status")
                if not isinstance(status, dict) or status.get("type") not in (
                    "idle", "active", "systemError", "notLoaded",
                ):
                    self._fail("APP_SERVER_INVALID_MESSAGE")
                if status["type"] == "systemError":
                    self._fail("APP_SERVER_REMOTE_ERROR")
                if status["type"] == "active":
                    if self.complete:
                        self._fail("APP_SERVER_EVENT_AFTER_COMPLETION")
                    if status.get("activeFlags") != []:
                        self._fail("APP_SERVER_APPROVAL_UNEXPECTED")
            if method == "thread/settings/updated":
                settings = params.get("threadSettings")
                if not isinstance(settings, dict):
                    self._fail("APP_SERVER_INVALID_MESSAGE")
                for key, expected in (("cwd", self._plan["cwd"]),
                                      ("model", self._plan["model"]),
                                      ("effort", "low"), ("approvalPolicy", "never")):
                    if key in settings and settings[key] != expected:
                        self._fail("APP_SERVER_CONFIGURATION_MISMATCH")
                if self._turn_id and settings.get("sandboxPolicy") != {
                    "type": "externalSandbox", "networkAccess": "restricted",
                }:
                    self._fail("APP_SERVER_CONFIGURATION_MISMATCH")
            return
        if method == "thread/closed":
            self._scope(params, turn=False)
            if not self.complete:
                self._fail("APP_SERVER_DISCONNECTED")
            return
        if self._state == "turn_start":
            if params.get("threadId") != self._thread_id:
                self._fail("APP_SERVER_THREAD_MISMATCH")
            self._buffer(message)
            return
        if self.complete and method not in {"thread/tokenUsage/updated"}:
            self._fail("APP_SERVER_EVENT_AFTER_COMPLETION")
        if method in {"turn/started", "turn/completed"}:
            self._scope(params, turn=False)
            turn = self._validate_turn(params.get("turn"), terminal=method == "turn/completed")
            if turn["id"] != self._turn_id:
                self._fail("APP_SERVER_TURN_MISMATCH")
            if method == "turn/started":
                if self._turn_started_seen:
                    self._fail("APP_SERVER_DUPLICATE_TURN_START")
                self._turn_started_seen = True
            else:
                self._finish_turn(turn)
            return
        self._scope(params, turn=True)
        if method in {"item/started", "item/completed"}:
            item = self._validate_item(params.get("item"))
            item_id = item["id"]
            if method == "item/started":
                if item_id in self._started_items or item_id in self._completed_items:
                    self._fail("APP_SERVER_DUPLICATE_ITEM")
                if len(self._started_items) >= MAX_ITEMS:
                    self._fail("APP_SERVER_ITEM_LIMIT")
                self._started_items.add(item_id)
            else:
                if item_id in self._completed_items:
                    self._fail("APP_SERVER_DUPLICATE_ITEM")
                self._record_item(item)
                self._completed_items.add(item_id)
            return
        if method == "thread/tokenUsage/updated":
            self._usage = self._validate_usage(params.get("tokenUsage"))
            return
        if method in _TURN_PROGRESS:
            if method in {"item/fileChange/outputDelta", "item/fileChange/patchUpdated",
                          "turn/diff/updated"} and not self._plan["allow_file_changes"]:
                self._fail("APP_SERVER_UNEXPECTED_ITEM")
            if method == "turn/diff/updated" and not isinstance(params.get("diff"), str):
                self._fail("APP_SERVER_INVALID_MESSAGE")
            if method == "item/fileChange/patchUpdated":
                if not _identifier(params.get("itemId")):
                    self._fail("APP_SERVER_INVALID_MESSAGE")
                self._validate_changes(params.get("changes"))
            if method.endswith("Delta") or method == "item/agentMessage/delta":
                if not isinstance(params.get("delta"), str) or not _identifier(params.get("itemId")):
                    self._fail("APP_SERVER_INVALID_MESSAGE")
            return
        self._fail("APP_SERVER_UNEXPECTED_NOTIFICATION")

    def _validate_turn(self, turn: Any, *, terminal: bool) -> dict:
        if (not isinstance(turn, dict) or not _identifier(turn.get("id"))
                or not isinstance(turn.get("items"), list)
                or len(turn["items"]) > MAX_ITEMS):
            self._fail("APP_SERVER_INVALID_MESSAGE")
        allowed = {"completed", "failed", "interrupted"} if terminal else {"inProgress"}
        if not isinstance(turn.get("status"), str) or turn["status"] not in allowed:
            self._fail("APP_SERVER_INVALID_TURN_STATUS")
        if terminal:
            view = turn.get("itemsView", "full")
            if view not in ("full", None, "notLoaded"):
                self._fail("APP_SERVER_PARTIAL_TURN")
            # The pinned server's completion notification is deliberately unloaded;
            # only already completed, correlated item events supply its final text.
            if view == "notLoaded" and turn["items"]:
                self._fail("APP_SERVER_PARTIAL_TURN")
        for item in turn["items"]:
            self._validate_item(item)
        return turn

    def _validate_item(self, item: Any) -> dict:
        if not isinstance(item, dict) or not _identifier(item.get("id")):
            self._fail("APP_SERVER_INVALID_MESSAGE")
        kind = item.get("type")
        allowed = {"agentMessage", "userMessage", "reasoning", "imageView"}
        if self._plan["allow_file_changes"]:
            allowed.add("fileChange")
        if not isinstance(kind, str) or kind not in allowed:
            self._fail("APP_SERVER_UNEXPECTED_ITEM")
        if kind == "agentMessage":
            if (not isinstance(item.get("text"), str)
                    or item.get("phase") not in (None, "commentary", "final_answer")):
                self._fail("APP_SERVER_INVALID_MESSAGE")
        if kind == "imageView" and (not isinstance(item.get("path"), str)
                                     or "\0" in item["path"]):
            self._fail("APP_SERVER_INVALID_MESSAGE")
        if kind == "fileChange":
            if item.get("status") not in ("inProgress", "completed", "failed", "declined"):
                self._fail("APP_SERVER_INVALID_MESSAGE")
            self._validate_changes(item.get("changes"))
        return item

    def _validate_changes(self, changes: Any) -> None:
        if not isinstance(changes, list) or len(changes) > MAX_ITEMS:
            self._fail("APP_SERVER_INVALID_MESSAGE")
        for change in changes:
            if (not isinstance(change, dict) or set(change) != {"path", "kind", "diff"}
                    or not isinstance(change["path"], str) or "\0" in change["path"]
                    or not isinstance(change["diff"], str)):
                self._fail("APP_SERVER_INVALID_MESSAGE")
            kind = change["kind"]
            if not isinstance(kind, dict) or kind.get("type") not in ("add", "delete", "update"):
                self._fail("APP_SERVER_INVALID_MESSAGE")
            # serde's enum rename_all changes variant tags, not fields inside
            # Update: the pinned Rust wire field is literally move_path.
            allowed = {"type", "move_path"} if kind["type"] == "update" else {"type"}
            if set(kind) - allowed:
                self._fail("APP_SERVER_INVALID_MESSAGE")
            if kind.get("move_path") is not None and (not isinstance(kind["move_path"], str)
                                                     or "\0" in kind["move_path"]):
                self._fail("APP_SERVER_INVALID_MESSAGE")

    def _record_item(self, item: dict) -> None:
        previous = self._items.get(item["id"])
        if previous is not None:
            keys = ("type", "text", "phase") if item["type"] == "agentMessage" else tuple(item)
            if any(previous.get(key) != item.get(key) for key in keys):
                self._fail("APP_SERVER_ITEM_CONFLICT")
        elif len(self._items) >= MAX_ITEMS:
            self._fail("APP_SERVER_ITEM_LIMIT")
        self._items[item["id"]] = item

    def _validate_usage(self, usage: Any) -> dict:
        if not isinstance(usage, dict) or set(usage) - {"last", "total", "modelContextWindow"}:
            self._fail("APP_SERVER_INVALID_USAGE")
        for key in ("last", "total"):
            counts = usage.get(key)
            if (not isinstance(counts, dict) or set(counts) != _TOKEN_KEYS
                    or any(type(value) is not int or not 0 <= value <= 9223372036854775807
                           for value in counts.values())):
                self._fail("APP_SERVER_INVALID_USAGE")
        window = usage.get("modelContextWindow")
        if window is not None and (type(window) is not int or not 0 <= window <= 9223372036854775807):
            self._fail("APP_SERVER_INVALID_USAGE")
        return copy.deepcopy(usage)

    def _finish_turn(self, turn: dict) -> None:
        self._turn_status = turn["status"]
        if self._turn_status != "completed" or turn.get("error") is not None:
            self._fail("APP_SERVER_TURN_INTERRUPTED" if self._turn_status == "interrupted"
                       else "APP_SERVER_TURN_FAILED")
        snapshot_ids = set()
        for item in turn["items"]:
            if item["id"] in snapshot_ids:
                self._fail("APP_SERVER_DUPLICATE_ITEM")
            snapshot_ids.add(item["id"])
            self._record_item(item)
        messages = [item for item in self._items.values() if item["type"] == "agentMessage"]
        explicit = [item for item in messages if item.get("phase") == "final_answer"]
        legacy = [item for item in messages if item.get("phase") is None]
        if explicit and legacy:
            self._fail("APP_SERVER_MULTIPLE_FINAL_OUTPUTS")
        finals = explicit if explicit else legacy
        if len(finals) != 1:
            self._fail("APP_SERVER_MULTIPLE_FINAL_OUTPUTS" if len(finals) > 1
                       else "APP_SERVER_MISSING_FINAL_OUTPUT")
        response = finals[0]["text"].encode("utf-8")
        if len(response) > self._response_limit:
            self._fail("APP_SERVER_RESPONSE_LIMIT")
        if not isinstance(_decode(response), dict):
            self._fail("APP_SERVER_INVALID_OUTPUT")
        self.response = response
        self.complete = True
        self._state = "completed"

    def finish_eof(self) -> None:
        if not self.complete:
            self._fail(self._error_code or "APP_SERVER_DISCONNECTED")

    def summary(self) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION, "thread_id": self._thread_id,
            "turn_id": self._turn_id, "turn_status": self._turn_status,
            "completed": self.complete, "response_bytes": len(self.response or b""),
            "usage": copy.deepcopy(self._usage),
            "usage_status": "unknown" if self._usage is None else "reported",
            "error_code": self._error_code, "frames": self._frames,
            "events": self._frames, "received_bytes": self._bytes,
            "buffered_events": len(self._early), "completed_items": len(self._items),
        }
