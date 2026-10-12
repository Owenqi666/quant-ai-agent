"""Pinned acquisition metadata sets; never read market originals or certify them."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import urllib.parse
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import alpha_vantage as collector

VERSION = "alpha-vantage-metadata-set-v1"
CALENDAR_VERSION = "alpha-vantage-set-calendar-v1"
MAX_METADATA_BYTES = 256 * 1024
VERSIONS = {"alpha-vantage-daily-acquisition-v1", "alpha-vantage-daily-acquisition-v1.1",
            "alpha-vantage-daily-acquisition-v1.2"}
FACTS = ("provider", "rights", "calendar", "units", "universe", "events", "vintage", "raw_identity")
REQUEST = {"function": "TIME_SERIES_DAILY", "outputsize": "compact", "datatype": "json"}
PUBLISHED_QUERY_URL = "https://www.nasdaqtrader.com/trader.aspx?id=calendar"
FAILURE_CODES = {"secret_reflected", "invalid_json", "duplicate_json_key", "nonfinite_json", "numeric_json_value",
                 "invalid_provider_shape", "provider_rejected", "invalid_provider_metadata", "provider_symbol_mismatch",
                 "provider_outputsize_mismatch", "invalid_provider_timezone", "invalid_record_count", "invalid_provider_date",
                 "future_provider_date", "invalid_bar_shape", "invalid_price_string", "invalid_volume_string",
                 "provider_refresh_mismatch", "no_completed_records", "redirect_rejected", "invalid_http_status",
                 "unsupported_content_encoding", "response_too_large", "invalid_response_bytes", "request_timeout",
                 "http_rejected", "transport_failed", "tls_certificate_failed", "tls_handshake_failed", "dns_failed",
                 "connection_failed", "tls_ca_unavailable"}


class AlphaVantageSetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise AlphaVantageSetError(code)


def _closed(value, keys, code):
    _need(type(value) is dict and set(value) == set(keys), code)


def _sha(value):
    _need(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "invalid_pin")


def _day(value):
    _need(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None, "invalid_date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise AlphaVantageSetError("invalid_date") from None


def _utc(value):
    _need(type(value) is str and len(value) <= 40, "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _need(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "invalid_timestamp")
        return parsed
    except ValueError:
        raise AlphaVantageSetError("invalid_timestamp") from None


def _symbols(value):
    _need(type(value) is list and 1 <= len(value) <= 3 and len(set(value)) == len(value) and
          all(type(s) is str and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", s) is not None for s in value),
          "invalid_assets")


def _output_path(path):
    full = collector._absolute(path)
    _need(full.suffix == ".json" and not full.name.casefold().endswith(".raw.json") and
          not any(p.casefold() in ("secrets", "credentials", "alpha-vantage-claims") or
                  p.casefold().endswith(".key") for p in full.parts), "output_role_conflict")
    return full


def _metadata(path, pin, role):
    _sha(pin)
    _need(type(path) is str and Path(path).is_absolute() and len(path) <= 2000, "absolute_metadata_path_required")
    full = collector._absolute(path)
    _need(not any(p.casefold() in ("secrets", "credentials", "alpha-vantage-claims") or
                  p.casefold().endswith(".key") for p in full.parts), "secret_path_refused")
    _need(full.name == "acquisition-manifest.json" if role == "manifest" else
          full.suffix == ".json" and "calendar" in full.stem.casefold() and "raw" not in full.stem.casefold(),
          "metadata_role_required")
    payload = collector._read(full, MAX_METADATA_BYTES)
    _need(hashlib.sha256(payload).hexdigest() == pin, "metadata_pin_mismatch")
    with collector._parent(full) as (parent, name, _):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    value = collector._json(payload)  # metadata integers only; floats/NaN rejected
    return value, {"path": str(full), "sha256": pin, "size_bytes": len(payload)}, (info.st_dev, info.st_ino)


def _calendar(value, start, end):
    keys = ("schema_version", "calendar_version", "exchange", "timezone", "start_date", "end_date", "sessions",
            "source_urls", "evidence_kind", "authentication", "exceptions")
    _closed(value, keys, "invalid_calendar")
    _need(type(value["schema_version"]) is int and value["schema_version"] == 1 and
          value["calendar_version"] == CALENDAR_VERSION and value["evidence_kind"] == "published_schedule" and
          value["authentication"] == "not_authenticated", "calendar_authority_refused")
    _need(type(value["exchange"]) is str and re.fullmatch(r"[A-Z][A-Z0-9_-]{1,15}", value["exchange"]) is not None,
          "invalid_calendar")
    _need(type(value["timezone"]) is str and len(value["timezone"]) <= 80, "invalid_calendar")
    try:
        ZoneInfo(value["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise AlphaVantageSetError("invalid_calendar") from None
    lower, upper = _day(value["start_date"]), _day(value["end_date"])
    _need(lower <= start <= end <= upper, "calendar_coverage_missing")
    sessions = value["sessions"]
    _need(type(sessions) is list and 1 <= len(sessions) <= 1024, "invalid_calendar_sessions")
    for day in sessions:
        _need(lower <= _day(day) <= upper, "invalid_calendar_sessions")
    _need(sessions == sorted(set(sessions)), "calendar_sessions_not_ordered_unique")
    expected = [s for s in sessions if start.isoformat() <= s <= end.isoformat()]
    _need(bool(expected), "no_requested_sessions")
    urls = value["source_urls"]
    _need(type(urls) is list and 1 <= len(urls) <= 5 and len(set(urls)) == len(urls), "invalid_calendar_urls")
    for url in urls:
        _need(type(url) is str and 0 < len(url) <= 1000, "invalid_calendar_urls")
        parsed = urllib.parse.urlsplit(url)
        _need(parsed.scheme == "https" and parsed.hostname is not None and parsed.username is None and
              parsed.password is None and (not parsed.query or url == PUBLISHED_QUERY_URL) and not parsed.fragment and
              not parsed.hostname.endswith("alphavantage.co"), "invalid_calendar_urls")
    exceptions = value["exceptions"]
    _need(type(exceptions) is list and len(exceptions) <= 100, "invalid_calendar_exceptions")
    for item in exceptions:
        _closed(item, ("date", "kind", "description", "authenticated"), "invalid_calendar_exceptions")
        _need(lower <= _day(item["date"]) <= upper and
              item["kind"] in ("early_close", "special_closure", "schedule_exception") and
              type(item["description"]) is str and 0 < len(item["description"]) <= 500 and
              item["authenticated"] is False, "invalid_calendar_exceptions")
    return expected


def _raw(value, attempt, symbol):
    _closed(value, ("relative_path", "sha256", "size_bytes"), "invalid_raw_descriptor")
    _sha(value["sha256"])
    _need(value["relative_path"] == "%02d-%s.raw.json" % (attempt, symbol) and
          type(value["size_bytes"]) is int and 0 < value["size_bytes"] <= collector.MAX_RESPONSE_BYTES,
          "invalid_raw_descriptor")


def _manifest(value):
    base = {"schema_version", "acquisition_version", "status", "code", "plan_id", "plan_sha256", "started_at_utc",
            "finished_at_utc", "provider", "safe_request", "request_endpoint", "attempt_count", "request_budget",
            "automatic_retries", "attempts", "responses", "code_identity", "documentation_urls",
            "documentation_snapshot_retained", "credential_recorded", "numeric_market_values_converted",
            "evaluation_performed", "experiment_test_partition_created", "model_calls", "db_mutations",
            "source_admission", "execution_allowed", "rights_calendar_events_units_venue_pit_authenticated"}
    _need(type(value) is dict and value.get("acquisition_version") in VERSIONS, "unsupported_acquisition_version")
    version = value["acquisition_version"]
    if version != "alpha-vantage-daily-acquisition-v1":
        base.add("tls_trust")
    if version == "alpha-vantage-daily-acquisition-v1.2":
        base.add("between_request_delay_seconds")
        _need(type(value.get("between_request_delay_seconds")) is int and value["between_request_delay_seconds"] == 2,
              "invalid_request_pacing")
    _closed(value, base, "invalid_manifest")
    _need(type(value["schema_version"]) is int and value["schema_version"] == 1 and
          value["provider"] == "Alpha Vantage" and value["safe_request"] == REQUEST and
          value["request_endpoint"] == collector.ENDPOINT and value["automatic_retries"] == 0 and
          value["source_admission"] == "pending", "invalid_manifest")
    _need(all(value[k] is False for k in ("documentation_snapshot_retained", "credential_recorded",
              "numeric_market_values_converted", "evaluation_performed", "experiment_test_partition_created",
              "execution_allowed", "rights_calendar_events_units_venue_pit_authenticated")) and
          type(value["model_calls"]) is int and value["model_calls"] == 0 and
          type(value["db_mutations"]) is int and value["db_mutations"] == 0, "manifest_boundary_violation")
    _sha(value["plan_sha256"])
    try:
        _need(str(uuid.UUID(value["plan_id"])) == value["plan_id"], "invalid_manifest")
    except (ValueError, TypeError, AttributeError):
        raise AlphaVantageSetError("invalid_manifest") from None
    start, finish = _utc(value["started_at_utc"]), _utc(value["finished_at_utc"])
    _need(start <= finish and value["documentation_urls"] == list(collector.DOCUMENTATION_URLS), "invalid_manifest")
    code = value["code_identity"]
    _closed(code, ("files", "sha256"), "invalid_code_identity")
    _closed(code["files"], collector.CODE_FILES, "invalid_code_identity")
    for pin in code["files"].values():
        _sha(pin)
    _need(code["sha256"] == collector._digest(code["files"]), "invalid_code_identity")
    if "tls_trust" in value:
        tls = value["tls_trust"]
        _closed(tls, ("ca_source", "certifi_version", "ca_bundle_sha256", "ca_bundle_size_bytes", "verify_mode",
                      "check_hostname", "session_key_logging"), "invalid_tls_identity")
        _sha(tls["ca_bundle_sha256"])
        _need(tls["ca_source"] == "certifi" and tls["verify_mode"] == "CERT_REQUIRED" and tls["check_hostname"] is True and
              tls["session_key_logging"] is False and type(tls["ca_bundle_size_bytes"]) is int and
              0 < tls["ca_bundle_size_bytes"] <= collector.MAX_RESPONSE_BYTES and type(tls["certifi_version"]) is str and
              re.fullmatch(r"[A-Za-z0-9.+-]{1,100}", tls["certifi_version"]) is not None, "invalid_tls_identity")
    attempts, responses = value["attempts"], value["responses"]
    _need(type(attempts) is list and type(responses) is list and type(value["attempt_count"]) is int and
          type(value["request_budget"]) is int and 1 <= len(attempts) == value["attempt_count"] <= value["request_budget"] <= 3,
          "invalid_attempt_count")
    seen, successful, prior_end = set(), {}, start
    for number, attempt in enumerate(attempts, 1):
        _closed(attempt, ("schema_version", "attempt", "symbol", "started_at_utc", "status", "safe_request",
                         "automatic_retries", "credential_recorded", "finished_at_utc", "http_status", "code", "raw",
                         "raw_response_suppressed", "provider_error_message_recorded"), "invalid_attempt")
        symbol = attempt["symbol"]
        _symbols([symbol])
        _need(symbol not in seen, "duplicate_attempt_asset")
        seen.add(symbol)
        begin, done = _utc(attempt["started_at_utc"]), _utc(attempt["finished_at_utc"])
        _need(prior_end <= begin <= done <= finish and attempt["schema_version"] == 1 and
              type(attempt["attempt"]) is int and attempt["attempt"] == number and
              attempt["safe_request"] == {**REQUEST, "symbol": symbol} and attempt["automatic_retries"] == 0 and
              attempt["credential_recorded"] is False and attempt["provider_error_message_recorded"] is False and
              type(attempt["raw_response_suppressed"]) is bool, "invalid_attempt")
        prior_end = done
        if attempt["raw"] is not None:
            _raw(attempt["raw"], number, symbol)
        if attempt["status"] == "downloaded":
            _need(attempt["http_status"] == 200 and attempt["code"] is None and attempt["raw"] is not None and
                  attempt["raw_response_suppressed"] is False, "false_download_success")
            successful[symbol] = attempt
        else:
            _need(attempt["status"] == "failed" and attempt["code"] in FAILURE_CODES and number == len(attempts),
                  "invalid_attempt_failure")
    if value["status"] == "downloaded_pending_admission":
        _need(value["code"] is None and len(successful) == len(attempts) == value["request_budget"], "false_batch_success")
    else:
        paced = version == "alpha-vantage-daily-acquisition-v1.2" and value["code"] == "request_pacing_interrupted"
        _need(value["status"] == "failed" and (paced and len(successful) == len(attempts) < value["request_budget"] or
              value["code"] == attempts[-1]["code"] and attempts[-1]["status"] == "failed"), "invalid_batch_failure")
    _need(len(responses) == len(successful), "success_response_count_mismatch")
    selected = {}
    for response in responses:
        _closed(response, ("symbol", "raw", "retrieved_at_utc", "provider_metadata", "record_count", "completed_record_count",
                           "completed_dates", "excluded_current_date_count", "completed_date_policy", "as_of_provider_date"),
                "invalid_response")
        symbol = response["symbol"]
        _need(symbol in successful and symbol not in selected, "unbound_or_duplicate_response")
        attempt = successful[symbol]
        _need(response["raw"] == attempt["raw"], "response_raw_binding_mismatch")
        retrieved = _utc(response["retrieved_at_utc"])
        _need(_utc(attempt["started_at_utc"]) <= retrieved <= _utc(attempt["finished_at_utc"]), "response_time_mismatch")
        meta = response["provider_metadata"]
        _closed(meta, collector.META_KEYS, "invalid_provider_metadata")
        _need(all(type(v) is str and 0 < len(v) <= 500 for v in meta.values()) and meta["2. Symbol"] == symbol and
              meta["4. Output Size"] == "Compact", "invalid_provider_metadata")
        try:
            tz = ZoneInfo(meta["5. Time Zone"])
            refreshed = datetime.fromisoformat(meta["3. Last Refreshed"]).date().isoformat()
        except (ValueError, ZoneInfoNotFoundError):
            raise AlphaVantageSetError("invalid_provider_metadata") from None
        today = retrieved.astimezone(tz).date().isoformat()
        _need(response["as_of_provider_date"] == today and
              response["completed_date_policy"] == "exclude_provider_timezone_current_date", "response_date_policy_mismatch")
        days = response["completed_dates"]
        _need(type(days) is list and 1 <= len(days) <= 100, "invalid_response_dates")
        for day in days:
            _need(_day(day).isoformat() < today, "unfinished_response_date")
        _need(days == sorted(set(days)), "response_dates_not_ordered_unique")
        count, excluded, total = response["completed_record_count"], response["excluded_current_date_count"], response["record_count"]
        _need(type(count) is int and count == len(days) and type(excluded) is int and excluded in (0, 1) and
              type(total) is int and total == count + excluded <= 100 and refreshed == (today if excluded else days[-1]),
              "response_count_mismatch")
        selected[symbol] = response
    return selected


def _original_status(path, descriptor, metadata_inodes):
    """Stat only. Never open/hash an original, even when its metadata pin exists."""
    try:
        with collector._parent(path) as (parent, name, _):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return "missing"
    except collector.AlphaVantageError:
        raise AlphaVantageSetError("unsafe_original_alias") from None
    _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid() and
          info.st_mode & 0o077 == 0 and (info.st_dev, info.st_ino) not in metadata_inodes, "unsafe_original_alias")
    return "present_size_match" if info.st_size == descriptor["size_bytes"] else "size_mismatch"


def build_acquisition_set(manifests, assets, start_date, end_date, calendar_path, calendar_sha256, out_path):
    """Build once from explicit pins/range; gaps never shrink the user's scope."""
    try:
        _symbols(assets)
        start, end = _day(start_date), _day(end_date)
        _need(start <= end and (end - start).days <= 1500, "invalid_requested_range")
        _need(type(manifests) is list and 1 <= len(manifests) <= 6, "explicit_manifests_required")
        for item in manifests:
            _closed(item, ("path", "sha256"), "invalid_manifest_reference")
        _need(len({item["path"] for item in manifests}) == len(manifests), "duplicate_manifest")
        out = _output_path(out_path)
        metadata_paths = {collector._absolute(item["path"]) for item in manifests}
        metadata_paths.add(collector._absolute(calendar_path))
        _need(out not in metadata_paths, "output_role_conflict")
        calendar, calendar_identity, inode = _metadata(calendar_path, calendar_sha256, "calendar")
        expected = _calendar(calendar, start, end)
        inodes, identities, responses = {inode}, [], {}
        for ref in manifests:
            manifest, identity, inode = _metadata(ref["path"], ref["sha256"], "manifest")
            _need(inode not in inodes, "metadata_alias")
            inodes.add(inode)
            successful = _manifest(manifest)
            # Reserve every original slot, including failed/missing originals
            # and attempts which suppressed their body. O_EXCL alone cannot
            # protect an absent original from a metadata report written there.
            root = Path(identity["path"]).parent
            original_slots = {root / ("%02d-%s.raw.json" % (a["attempt"], a["symbol"]))
                              for a in manifest["attempts"]}
            original_slots.update(root / a["raw"]["relative_path"] for a in manifest["attempts"] if a["raw"] is not None)
            original_slots.update(root / r["raw"]["relative_path"] for r in manifest["responses"])
            _need(out not in original_slots, "output_role_conflict")
            identities.append({**identity, "acquisition_version": manifest["acquisition_version"],
                               "batch_status": manifest["status"], "attempt_count": manifest["attempt_count"]})
            for asset, response in successful.items():
                _need(asset not in responses, "duplicate_successful_asset")
                responses[asset] = (response, identity)
        expected_set, selected, missing = set(expected), [], []
        for asset in assets:
            if asset not in responses:
                missing.append("asset:" + asset + ":successful_response")
                selected.append({"asset": asset, "status": "missing_successful_response", "raw": None,
                                 "missing_expected_sessions": list(expected)})
                continue
            response, manifest_identity = responses[asset]
            days = [d for d in response["completed_dates"] if start_date <= d <= end_date]
            gaps = sorted(expected_set - set(days))
            unexpected = sorted(set(days) - expected_set)
            _need(not unexpected, "response_calendar_conflict")
            raw = response["raw"]
            original = Path(manifest_identity["path"]).parent / raw["relative_path"]
            presence = _original_status(original, raw, inodes)
            provider_zone = response["provider_metadata"]["5. Time Zone"]
            matching_zone = provider_zone == calendar["timezone"] or \
                {provider_zone, calendar["timezone"]} <= {"US/Eastern", "America/New_York"}
            if gaps:
                missing.append("asset:" + asset + ":expected_sessions")
            if presence != "present_size_match":
                missing.append("asset:" + asset + ":original_" + presence)
            if not matching_zone:
                missing.append("asset:" + asset + ":calendar_timezone_review")
            selected.append({"asset": asset, "status": "metadata_complete" if not gaps and matching_zone and presence == "present_size_match" else "needs_inputs",
                             "manifest": manifest_identity, "raw": {**raw, "path": str(original)},
                             "original_status": presence, "original_byte_identity_verified": False,
                             "retrieved_at_utc": response["retrieved_at_utc"], "as_of_provider_date": response["as_of_provider_date"],
                             "provider_timezone": provider_zone, "calendar_timezone_labels_compatible": matching_zone,
                             "in_scope_completed_dates": days, "missing_expected_sessions": gaps,
                             "provider_completed_record_count": response["completed_record_count"]})
        report = {"schema_version": 1, "set_version": VERSION,
                  "status": "needs_inputs" if missing else "metadata_complete_pending_admission",
                  "assets": list(assets), "requested_range": {"start_date": start_date, "end_date": end_date},
                  "scope_automatically_shrunk": False, "expected_sessions": expected,
                  "calendar": {**calendar_identity, "exchange": calendar["exchange"], "timezone": calendar["timezone"],
                               "source_urls": calendar["source_urls"], "evidence_kind": "published_schedule",
                               "authentication": "not_authenticated", "exception_count": len(calendar["exceptions"]),
                               "exception_coverage": "not_authenticated"},
                  "manifests": identities, "selected": selected, "missing_inputs": missing,
                  "successful_requested_assets": sum(item["raw"] is not None for item in selected),
                  "pending_source_facts": list(FACTS), "source_admission": "pending", "execution_allowed": False,
                  "market_originals_opened": 0, "market_originals_hashed": 0, "numeric_market_values_converted": False,
                  "evaluation_performed": False, "experiment_test_partition_created": False,
                  "model_calls": 0, "network_requests": 0, "db_mutations": 0,
                  "pinned_metadata_authenticates_source_or_venue": False}
        # Recheck only metadata before publishing. Market originals remain stat-only.
        for ref in manifests:
            _metadata(ref["path"], ref["sha256"], "manifest")
        _metadata(calendar_path, calendar_sha256, "calendar")
        collector._write_new(out, collector._canonical(report) + b"\n")
        return report
    except AlphaVantageSetError:
        raise
    except collector.AlphaVantageError as exc:
        raise AlphaVantageSetError(exc.code) from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise AlphaVantageSetError("invalid_metadata_set") from None
