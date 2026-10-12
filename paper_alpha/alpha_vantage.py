"""Bounded raw Alpha Vantage acquisition; never source or execution authority.

Credentials stay in an owner-only local file. No function logs a request URL,
credential, provider error message, numeric market row, or original exception.
The downloaded bytes require independent admission before any computation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "alpha-vantage-daily-acquisition-v1.2"
BETWEEN_REQUEST_DELAY_SECONDS = 2
ENDPOINT = "https://www.alphavantage.co/query"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PLAN_BYTES = 64 * 1024
MAX_REQUESTS = 3
DOCUMENTATION_URLS = (
    "https://www.alphavantage.co/documentation/#daily",
    "https://www.alphavantage.co/support/#api-key",
    "https://www.alphavantage.co/terms_of_service/",
)
CODE_FILES = ("paper_alpha/alpha_vantage.py", "scripts/alpha_vantage_daily.py")
META_KEYS = ("1. Information", "2. Symbol", "3. Last Refreshed", "4. Output Size", "5. Time Zone")
BAR_KEYS = ("1. open", "2. high", "3. low", "4. close", "5. volume")


class AlphaVantageError(ValueError):
    """Only a static safe code may cross the connector boundary."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise AlphaVantageError(code)


def _now(clock=None):
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    _need(isinstance(value, datetime) and value.tzinfo is not None, "invalid_clock")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    return value.isoformat().replace("+00:00", "Z")


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _pairs(items):
    out = {}
    for key, value in items:
        _need(key not in out, "duplicate_json_key")
        out[key] = value
    return out


def _reject_number(_):
    raise AlphaVantageError("numeric_json_value")


def _json(payload, *, provider=False):
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_int=_reject_number if provider else int,
                          parse_float=_reject_number,
                          parse_constant=lambda _: (_ for _ in ()).throw(AlphaVantageError("nonfinite_json")))
    except AlphaVantageError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise AlphaVantageError("invalid_json") from None


def _absolute(path):
    value = Path(path)
    if not value.is_absolute():
        value = Path.cwd() / value
    _need(".." not in value.parts and "\x00" not in str(value), "unsafe_path")
    return value


def _safe_directory(info):
    _need(stat.S_ISDIR(info.st_mode), "unsafe_directory")
    writable = info.st_mode & 0o022
    # A root-owned sticky /private/tmp ancestor is safe for private children.
    _need(not writable or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX), "unsafe_directory")


@contextmanager
def _parent(path, *, create=False, private=False):
    """Walk directory fds without following symlinks or a path substitution."""
    path = _absolute(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open("/", flags)
        for part in path.parent.parts[1:]:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                _need(create, "path_missing")
                os.mkdir(part, 0o700, dir_fd=fd)
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            _safe_directory(os.fstat(fd))
        if private:
            info = os.fstat(fd)
            _need(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
                  "credential_parent_not_private")
        yield fd, path.name, path
    except AlphaVantageError:
        raise
    except OSError:
        raise AlphaVantageError("path_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _file_info(info, *, immutable=False):
    _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1,
          "unsafe_file")
    _need(info.st_mode & 0o077 == 0, "file_not_private")
    if immutable:
        _need(info.st_mode & 0o222 == 0, "plan_not_frozen")


def _read(path, limit, *, private_parent=False, immutable=False):
    with _parent(path, private=private_parent) as (parent, name, _):
        fd = None
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            info = os.fstat(fd)
            _file_info(info, immutable=immutable)
            _need(0 < info.st_size <= limit, "file_size_out_of_bounds")
            with os.fdopen(fd, "rb") as stream:
                fd = None
                payload = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
            _need(len(payload) <= limit and info.st_size == len(payload) and
                  (info.st_ino, info.st_size, info.st_mtime_ns) ==
                  (after.st_ino, after.st_size, after.st_mtime_ns), "input_changed")
            return payload
        except AlphaVantageError:
            raise
        except OSError:
            raise AlphaVantageError("file_unavailable") from None
        finally:
            if fd is not None:
                os.close(fd)


def _write_at(parent, name, payload, *, mode=0o400):
    _need("/" not in name and name not in ("", ".", ".."), "unsafe_path")
    fd = None
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.fsync(parent)
    except OSError:
        raise AlphaVantageError("output_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _write_new(path, payload, *, mode=0o400, private_parent=False):
    with _parent(path, create=True, private=private_parent) as (parent, name, full):
        _write_at(parent, name, payload, mode=mode)
        return full


def _valid_key(key):
    return type(key) is str and re.fullmatch(r"[A-Za-z0-9]{8,128}", key) is not None


def configure_credential(path, key):
    """Called by a hidden getpass prompt; key is never a CLI argument."""
    _need(_valid_key(key), "invalid_credential")
    _write_new(path, (key + "\n").encode("ascii"), mode=0o600, private_parent=True)
    return {"status": "credential_configured", "credential_private": True, "network_requests": 0}


def _credential(path):
    payload = _read(path, 130, private_parent=True)
    try:
        key = payload.decode("ascii").removesuffix("\n")
    except UnicodeError:
        raise AlphaVantageError("invalid_credential") from None
    _need(_valid_key(key), "invalid_credential")
    return key


def credential_status(path):
    """Inspect existence and local usability, never account entitlement."""
    exists = False
    try:
        with _parent(path, private=True) as (parent, name, _):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                return {"status": "not_configured", "credential_exists": False,
                        "credential_private": False, "credential_usable": False}
            _file_info(info)
        _credential(path)
        return {"status": "configured", "credential_exists": True,
                "credential_private": True, "credential_usable": True,
                "remote_entitlement_checked": False}
    except AlphaVantageError as exc:
        if exc.code == "path_missing":
            return {"status": "not_configured", "credential_exists": False,
                    "credential_private": False, "credential_usable": False}
        return {"status": "unavailable", "credential_exists": exists,
                "credential_private": False, "credential_usable": False, "code": exc.code}


def _code_identity():
    root = Path(__file__).resolve().parents[1]
    try:
        files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in CODE_FILES}
    except OSError:
        raise AlphaVantageError("code_identity_unavailable") from None
    return {"files": files, "sha256": _digest(files)}


def _symbols(value):
    _need(type(value) is list and 1 <= len(value) <= MAX_REQUESTS, "invalid_symbols")
    _need(all(type(item) is str and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", item)
              for item in value), "invalid_symbols")
    _need(len(set(value)) == len(value), "duplicate_symbols")


def _ca_bytes(path):
    """Read only the installed public CA bundle, never a credential file."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        _need(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_RESPONSE_BYTES and
              info.st_mode & 0o022 == 0, "tls_ca_unavailable")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            payload = stream.read(MAX_RESPONSE_BYTES + 1)
            after = os.fstat(stream.fileno())
        _need(len(payload) == info.st_size and len(payload) <= MAX_RESPONSE_BYTES and
              (info.st_ino, info.st_size, info.st_mtime_ns) ==
              (after.st_ino, after.st_size, after.st_mtime_ns), "tls_ca_unavailable")
        return payload
    except AlphaVantageError:
        raise
    except (OSError, ValueError, TypeError):
        raise AlphaVantageError("tls_ca_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _tls_configuration():
    """Prepare verified TLS before a Key is read or any request is attempted.

    certifi is an explicit optional data dependency. Its public certificate
    bytes and installed version are local reproducibility pins, not evidence
    that the acquired market source is admitted or licensed.
    """
    try:
        certifi = importlib.import_module("certifi")
        version = metadata.version("certifi")
        _need(type(version) is str and re.fullmatch(r"[A-Za-z0-9.+-]{1,100}", version) is not None,
              "tls_ca_unavailable")
        bundle_path = certifi.where()
        payload = _ca_bytes(bundle_path)
        context = ssl.create_default_context(cafile=bundle_path)
        # create_default_context can honor SSLKEYLOGFILE. No TLS session secret
        # may be logged: disable it explicitly before any possible handshake.
        context.keylog_filename = None
        _need(context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True and
              context.cert_store_stats().get("x509_ca", 0) > 0 and
              context.keylog_filename is None, "tls_ca_unavailable")
        _need(_ca_bytes(bundle_path) == payload, "tls_ca_unavailable")
        identity = {"ca_source": "certifi", "certifi_version": version,
                    "ca_bundle_sha256": hashlib.sha256(payload).hexdigest(),
                    "ca_bundle_size_bytes": len(payload), "verify_mode": "CERT_REQUIRED",
                    "check_hostname": True, "session_key_logging": False}
        return identity, context
    except AlphaVantageError:
        raise
    except (ImportError, metadata.PackageNotFoundError, OSError, ValueError, TypeError, AttributeError):
        raise AlphaVantageError("tls_ca_unavailable") from None


def create_plan(path, symbols, *, timeout_seconds=20, clock=None):
    symbols = list(symbols)
    _symbols(symbols)
    _need(type(timeout_seconds) is int and 1 <= timeout_seconds <= 30, "invalid_timeout")
    tls_trust, _ = _tls_configuration()
    plan = {"schema_version": 1, "acquisition_version": VERSION, "plan_id": str(uuid.uuid4()),
            "created_at_utc": _timestamp(_now(clock)), "frozen_plan_path": str(_absolute(path)),
            "endpoint": ENDPOINT,
            "request": {"function": "TIME_SERIES_DAILY", "outputsize": "compact", "datatype": "json"},
            "symbols": symbols, "max_requests": len(symbols), "timeout_seconds": timeout_seconds,
            "max_response_bytes": MAX_RESPONSE_BYTES, "automatic_retries": 0, "follow_redirects": False,
            "between_request_delay_seconds": BETWEEN_REQUEST_DELAY_SECONDS,
            "completed_date_policy": "exclude_provider_timezone_current_date",
            "code_identity": _code_identity(), "documentation_urls": list(DOCUMENTATION_URLS),
            "tls_trust": tls_trust,
            "documentation_snapshot_retained": False, "source_admission": "pending",
            "execution_allowed": False}
    plan["plan_sha256"] = _digest(plan)
    plan_bytes = _canonical(plan) + b"\n"
    full = _write_new(path, plan_bytes)
    file_pin = hashlib.sha256(plan_bytes).hexdigest()
    receipt = {"schema_version": 1, "acquisition_version": VERSION,
               "plan_id": plan["plan_id"], "frozen_plan_path": str(full),
               "plan_file_sha256": file_pin, "size_bytes": len(plan_bytes),
               "local_integrity_only": True, "source_authority": False}
    _write_new(full.with_name(full.name + ".receipt.json"), _canonical(receipt) + b"\n")
    return {"status": "planned", "plan_path": str(full), "max_requests": len(symbols),
            "plan_file_sha256": file_pin, "network_requests": 0,
            "source_admission": "pending", "execution_allowed": False}


def _load_plan(path, *, include_tls_context=False):
    payload = _read(path, MAX_PLAN_BYTES, immutable=True)
    path = _absolute(path)
    receipt = _json(_read(path.with_name(path.name + ".receipt.json"), MAX_PLAN_BYTES, immutable=True))
    receipt_keys = {"schema_version", "acquisition_version", "plan_id", "frozen_plan_path",
                    "plan_file_sha256", "size_bytes", "local_integrity_only", "source_authority"}
    _need(type(receipt) is dict and set(receipt) == receipt_keys and
          type(receipt["schema_version"]) is int and receipt["schema_version"] == 1 and
          receipt["acquisition_version"] == VERSION and receipt["local_integrity_only"] is True and
          receipt["source_authority"] is False, "invalid_plan_receipt")
    _need(receipt["frozen_plan_path"] == str(path) and
          type(receipt["size_bytes"]) is int and receipt["size_bytes"] == len(payload) and
          receipt["plan_file_sha256"] == hashlib.sha256(payload).hexdigest(), "plan_file_pin_mismatch")
    plan = _json(payload)
    keys = {"schema_version", "acquisition_version", "plan_id", "created_at_utc", "frozen_plan_path", "endpoint", "request",
            "symbols", "max_requests", "timeout_seconds", "max_response_bytes", "automatic_retries",
            "follow_redirects", "completed_date_policy", "code_identity", "documentation_urls",
            "documentation_snapshot_retained", "source_admission", "execution_allowed", "plan_sha256", "tls_trust",
            "between_request_delay_seconds"}
    _need(type(plan) is dict and set(plan) == keys, "invalid_plan")
    pin = plan["plan_sha256"]
    unsigned = {k: v for k, v in plan.items() if k != "plan_sha256"}
    _need(type(pin) is str and re.fullmatch(r"[a-f0-9]{64}", pin) is not None and
          pin == _digest(unsigned), "plan_pin_mismatch")
    _need(type(plan["schema_version"]) is int and plan["schema_version"] == 1 and
          plan["acquisition_version"] == VERSION, "unsupported_plan")
    _need(plan["plan_id"] == receipt["plan_id"], "plan_receipt_identity_mismatch")
    # Local file binding prevents accidental replay through copied/renamed
    # plans. This is reproducibility protection, never signed authority.
    _need(plan["frozen_plan_path"] == str(_absolute(path)), "plan_path_changed")
    _need(plan["endpoint"] == ENDPOINT and plan["request"] ==
          {"function": "TIME_SERIES_DAILY", "outputsize": "compact", "datatype": "json"}, "unsafe_request")
    _symbols(plan["symbols"])
    _need(type(plan["max_requests"]) is int and plan["max_requests"] == len(plan["symbols"]) and
          type(plan["timeout_seconds"]) is int and 1 <= plan["timeout_seconds"] <= 30 and
          type(plan["max_response_bytes"]) is int and plan["max_response_bytes"] == MAX_RESPONSE_BYTES and
          type(plan["between_request_delay_seconds"]) is int and
          plan["between_request_delay_seconds"] == BETWEEN_REQUEST_DELAY_SECONDS,
          "unsafe_budget")
    _need(type(plan["automatic_retries"]) is int and plan["automatic_retries"] == 0 and
          plan["follow_redirects"] is False and
          plan["completed_date_policy"] == "exclude_provider_timezone_current_date" and
          plan["documentation_urls"] == list(DOCUMENTATION_URLS) and
          plan["documentation_snapshot_retained"] is False and
          plan["source_admission"] == "pending" and plan["execution_allowed"] is False, "unsafe_plan")
    try:
        _need(str(uuid.UUID(plan["plan_id"])) == plan["plan_id"], "invalid_plan")
        created = datetime.fromisoformat(plan["created_at_utc"].replace("Z", "+00:00"))
        _need(created.tzinfo is not None and created.utcoffset().total_seconds() == 0, "invalid_plan")
    except (TypeError, ValueError, AttributeError):
        raise AlphaVantageError("invalid_plan") from None
    _need(plan["code_identity"] == _code_identity(), "code_identity_changed")
    tls_trust, context = _tls_configuration()
    _need(plan["tls_trust"] == tls_trust, "tls_identity_changed")
    if include_tls_context:
        return plan, payload, context
    return plan, payload


def read_plan(path):
    return _load_plan(path)[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(request, timeout, *, context=None):
    # Proxy settings are intentionally disabled: never forward this Key to a
    # user-configured proxy or redirected endpoint. TLS uses the verified, frozen certifi context.
    if context is None:
        _, context = _tls_configuration()
    _need(context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True and
          context.keylog_filename is None, "tls_ca_unavailable")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=context))
    return opener.open(request, timeout=timeout)


def _transport_error_code(error):
    """Classify trusted exception types; never inspect a reason's text."""
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls_certificate_failed"
    if isinstance(reason, ssl.SSLError):
        return "tls_handshake_failed"
    if isinstance(reason, socket.gaierror):
        return "dns_failed"
    if isinstance(reason, TimeoutError):
        return "request_timeout"
    if isinstance(reason, ConnectionError):
        return "connection_failed"
    return "transport_failed"


def _contains_secret(value, key):
    if type(value) is str:
        return key in value
    if type(value) is dict:
        return any(_contains_secret(k, key) or _contains_secret(v, key) for k, v in value.items())
    if type(value) is list:
        return any(_contains_secret(item, key) for item in value)
    return False


def _inspect_body(payload, key):
    _need(type(payload) is bytes and len(payload) <= MAX_RESPONSE_BYTES, "response_too_large")
    if key.encode("ascii") in payload:
        return None, True, "secret_reflected"
    try:
        # Alpha Vantage's documented OHLCV fields are strings. Reject numeric
        # tokens before conversion, including unexpected provider fields.
        value = _json(payload, provider=True)
    except AlphaVantageError as exc:
        # Do not retain malformed text: it may contain an encoded credential.
        return None, True, exc.code
    if _contains_secret(value, key):
        return None, True, "secret_reflected"
    return value, False, None


def _read_response(response, timeout, request_started):
    """Cap bytes and the real stdlib socket's wall-clock read allowance."""
    chunks = []
    total = 0
    reader = getattr(response, "read1", response.read)
    while total <= MAX_RESPONSE_BYTES:
        remaining = timeout - (time.monotonic() - request_started)
        _need(remaining > 0, "request_timeout")
        # HTTPResponse exposes the socket through fp.raw; HTTPError wraps that
        # HTTPResponse one level deeper. Fake offline responses need no socket.
        candidate = response
        for _ in range(4):
            sock = getattr(candidate, "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
                break
            candidate = getattr(candidate, "raw", None) or getattr(candidate, "fp", None)
            if candidate is None:
                break
        chunk = reader(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - total))
        _need(type(chunk) is bytes, "invalid_response_bytes")
        _need(time.monotonic() - request_started <= timeout, "request_timeout")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        _need(total <= MAX_RESPONSE_BYTES, "response_too_large")
    raise AlphaVantageError("response_too_large")


def _daily_shape(value, symbol, retrieved):
    _need(type(value) is dict, "invalid_provider_shape")
    _need(not any(k in value for k in ("Error Message", "Information", "Note")), "provider_rejected")
    _need(set(value) == {"Meta Data", "Time Series (Daily)"}, "invalid_provider_shape")
    meta, bars = value["Meta Data"], value["Time Series (Daily)"]
    _need(type(meta) is dict and set(meta) == set(META_KEYS) and
          all(type(v) is str and 0 < len(v) <= 500 for v in meta.values()), "invalid_provider_metadata")
    _need(meta["2. Symbol"] == symbol, "provider_symbol_mismatch")
    _need(meta["4. Output Size"] == "Compact", "provider_outputsize_mismatch")
    try:
        provider_tz = ZoneInfo(meta["5. Time Zone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise AlphaVantageError("invalid_provider_timezone") from None
    provider_today = retrieved.astimezone(provider_tz).date()
    _need(type(bars) is dict and 1 <= len(bars) <= 100, "invalid_record_count")
    days = []
    for day, row in bars.items():
        _need(type(day) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is not None,
              "invalid_provider_date")
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError:
            raise AlphaVantageError("invalid_provider_date") from None
        _need(parsed_day <= provider_today, "future_provider_date")
        _need(type(row) is dict and set(row) == set(BAR_KEYS), "invalid_bar_shape")
        for field in BAR_KEYS[:4]:
            text = row[field]
            _need(type(text) is str and len(text) <= 64 and
                  re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", text) is not None and
                  bool(text.replace(".", "").strip("0")), "invalid_price_string")
        volume = row[BAR_KEYS[4]]
        _need(type(volume) is str and len(volume) <= 24 and
              re.fullmatch(r"(?:0|[1-9][0-9]*)", volume) is not None, "invalid_volume_string")
        days.append(day)
    _need(re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: [0-9]{2}:[0-9]{2}:[0-9]{2})?",
                       meta["3. Last Refreshed"]) is not None, "invalid_provider_metadata")
    try:
        datetime.fromisoformat(meta["3. Last Refreshed"])
    except ValueError:
        raise AlphaVantageError("invalid_provider_metadata") from None
    _need(meta["3. Last Refreshed"][:10] == max(days), "provider_refresh_mismatch")
    completed = sorted(day for day in days if day < provider_today.isoformat())
    _need(bool(completed), "no_completed_records")
    return {"provider_metadata": dict(meta), "record_count": len(days), "completed_record_count": len(completed),
            "completed_dates": completed, "excluded_current_date_count": len(days) - len(completed),
            "completed_date_policy": "exclude_provider_timezone_current_date",
            "as_of_provider_date": provider_today.isoformat()}


def _draft(plan, retrieved):
    # Import the actual project template; no guessed incompatible eight facts.
    from .daily_real_preparation import source_preparation_template
    result = source_preparation_template()
    result["source_id"] = "alpha-vantage-" + plan["plan_id"]
    result["intended_use"] = "Local personal research and data admission preparation; rights unverified."
    result["source_details"]["provider"] = "Alpha Vantage"
    result["source_details"]["source_version"] = "TIME_SERIES_DAILY compact; provider revision unspecified"
    result["source_details"]["retrieved_at_utc"] = _timestamp(retrieved)
    # Multiple response files are pins in the acquisition manifest, not one
    # counterfeit raw descriptor. Calendar/selection/events/units stay unknown.
    result["raw"] = None
    return result


def fetch_plan(plan_path, credential_path, out_path, *, transport=None, clock=None):
    """Explicit opt-in acquisition only. One irreversible claim per plan.

    Injectable transport(request, timeout) must return a response supporting
    read(n), status (or getcode()), headers, and optional close()/geturl().
    Tests must use fabricated bytes; this function never computes prices.
    """
    plan, plan_bytes, tls_context = _load_plan(plan_path, include_tls_context=True)
    key = _credential(credential_path)
    _need(key.encode("ascii") not in plan_bytes and not _contains_secret(plan, key), "secret_in_metadata")
    out = _absolute(out_path)
    _need(key not in str(out) and key not in str(_absolute(plan_path)), "secret_in_path")
    started = _now(clock)
    fd = None
    with _parent(out, create=True) as (parent, name, _):
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        except OSError:
            raise AlphaVantageError("output_unavailable") from None
    try:
        # Never issue a second run of this plan, including after a crash or
        # supplier failure. A newly reviewed plan is necessary for another run.
        claim = {"schema_version": 1, "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"],
                 "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
                 "out_path": str(out), "started_at_utc": _timestamp(started),
                 "request_budget": plan["max_requests"], "local_integrity_only": True}
        # One namespace per local credential configuration prevents a cloned
        # plan with a forged new sidecar from consuming the same UUID twice.
        # This is local accidental-replay protection, not supplier authority.
        claim_path = _absolute(credential_path).parent / "alpha-vantage-claims" / (plan["plan_id"] + ".json")
        _write_new(claim_path, _canonical(claim) + b"\n", private_parent=True)
        _write_at(fd, "plan.json", plan_bytes)
        records, attempts = [], []
        failure = None
        for index, symbol in enumerate(plan["symbols"], 1):
            if index > 1:
                try:
                    # Wait after the previous response completes. This is
                    # pacing within one fetch, never a failed-request retry
                    # or a claim about other clients using the same account.
                    time.sleep(plan["between_request_delay_seconds"])
                except (Exception, KeyboardInterrupt):
                    failure = "request_pacing_interrupted"
                    break
            attempt = {"schema_version": 1, "attempt": index, "symbol": symbol,
                       "started_at_utc": _timestamp(_now(clock)), "status": "started",
                       "safe_request": {**plan["request"], "symbol": symbol}, "automatic_retries": 0,
                       "credential_recorded": False}
            _write_at(fd, "attempt-%02d.started.json" % index, _canonical(attempt) + b"\n")
            # The request becomes attempted immediately before entering the
            # transport; timeout/connect/HTTP failures still consume one slot.
            attempts.append(attempt)
            response = None
            http_status = None
            payload = None
            shape = None
            suppression = False
            code = None
            retrieved = None
            try:
                query = {**plan["request"], "symbol": symbol, "apikey": key}
                request = urllib.request.Request(ENDPOINT + "?" + urllib.parse.urlencode(query),
                                                 headers={"Accept": "application/json"}, method="GET")
                request_started = time.monotonic()
                try:
                    response = (transport(request, plan["timeout_seconds"]) if transport is not None
                                else _transport(request, plan["timeout_seconds"], context=tls_context))
                except urllib.error.HTTPError as exc:
                    response = exc
                http_status = getattr(response, "status", None)
                if http_status is None:
                    http_status = response.getcode()
                _need(type(http_status) is int and 100 <= http_status <= 599, "invalid_http_status")
                if 300 <= http_status <= 399:
                    raise AlphaVantageError("redirect_rejected")
                final_url = response.geturl() if hasattr(response, "geturl") else request.full_url
                _need(final_url == request.full_url, "redirect_rejected")
                headers = getattr(response, "headers", {})
                encoding = headers.get("Content-Encoding", "identity")
                _need(encoding in (None, "", "identity"), "unsupported_content_encoding")
                length = headers.get("Content-Length")
                if length is not None:
                    _need(type(length) is str and re.fullmatch(r"[0-9]{1,10}", length) is not None and
                          int(length) <= MAX_RESPONSE_BYTES, "response_too_large")
                payload = _read_response(response, plan["timeout_seconds"], request_started)
                value, suppression, body_code = _inspect_body(payload, key)
                if body_code:
                    raise AlphaVantageError(body_code)
                if http_status != 200:
                    raise AlphaVantageError("http_rejected")
                retrieved = _now(clock)
                shape = _daily_shape(value, symbol, retrieved)
            except AlphaVantageError as exc:
                code = exc.code
                if code in ("response_too_large", "invalid_response_bytes"):
                    suppression = True
            except (Exception, KeyboardInterrupt) as exc:
                # Original urllib errors often contain the secret-bearing URL.
                # Deliberately do not stringify, chain, serialize or hash them.
                code = _transport_error_code(exc)
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
            raw = None
            if payload is not None and not suppression and len(payload) <= MAX_RESPONSE_BYTES:
                # Validate again before hashing or writing a body even when an
                # HTTP failure prevented successful daily-shape validation.
                _, unsafe, _ = _inspect_body(payload, key)
                suppression = unsafe
                if not unsafe:
                    raw_name = "%02d-%s.raw.json" % (index, symbol)
                    _write_at(fd, raw_name, payload)
                    raw = {"relative_path": raw_name, "sha256": hashlib.sha256(payload).hexdigest(),
                           "size_bytes": len(payload)}
            attempt = {**attempt, "finished_at_utc": _timestamp(_now(clock)),
                       "status": "downloaded" if code is None else "failed", "http_status": http_status,
                       "code": code, "raw": raw, "raw_response_suppressed": suppression,
                       "provider_error_message_recorded": False}
            attempts[-1] = attempt
            _write_at(fd, "attempt-%02d.json" % index, _canonical(attempt) + b"\n")
            if code is None:
                records.append({"symbol": symbol, "raw": raw, "retrieved_at_utc": _timestamp(retrieved),
                                **shape})
            else:
                failure = code
                break
        finished = _now(clock)
        receipt = {"schema_version": 1, "acquisition_version": VERSION,
                   "status": "downloaded_pending_admission" if failure is None else "failed",
                   "code": failure, "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"],
                   "started_at_utc": _timestamp(started), "finished_at_utc": _timestamp(finished),
                   "provider": "Alpha Vantage", "safe_request": dict(plan["request"]),
                   "request_endpoint": ENDPOINT, "attempt_count": len(attempts),
                   "request_budget": plan["max_requests"], "automatic_retries": 0,
                   "between_request_delay_seconds": plan["between_request_delay_seconds"],
                   "attempts": attempts, "responses": records,
                   "code_identity": plan["code_identity"], "documentation_urls": list(DOCUMENTATION_URLS),
                   "tls_trust": plan["tls_trust"],
                   "documentation_snapshot_retained": False, "credential_recorded": False,
                   "numeric_market_values_converted": False, "evaluation_performed": False,
                   "experiment_test_partition_created": False, "model_calls": 0, "db_mutations": 0,
                   "source_admission": "pending", "execution_allowed": False,
                   "rights_calendar_events_units_venue_pit_authenticated": False}
        _write_at(fd, "acquisition-manifest.json", _canonical(receipt) + b"\n")
        _write_at(fd, "source-preparation-draft.json", _canonical(_draft(plan, finished)) + b"\n")
        return {"status": receipt["status"], "code": failure, "out_path": str(out),
                "manifest_path": str(out / "acquisition-manifest.json"), "attempt_count": len(attempts),
                "downloaded_symbols": len(records), "source_admission": "pending", "execution_allowed": False}
    finally:
        if fd is not None:
            os.close(fd)
