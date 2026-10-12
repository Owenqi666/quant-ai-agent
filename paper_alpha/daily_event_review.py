"""Bounded, metadata-only corporate-event declarations; never authorization.

The two public functions perform no I/O. Evidence URLs are retained references,
not fetched or authenticated. A complete declaration cannot prove absence.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import re
from urllib.parse import urlsplit

VERSION = "daily-event-coverage-review-v1"
EVENT_TYPES = ("split", "dividend", "halt", "no_quote", "listing", "delisting")
POLICY = "reject_unsupported_development_events"
MAX_JSON_BYTES = 256 * 1024
MAX_ASSETS = 128
MAX_SESSIONS = 1024
MAX_EVIDENCE = 512
MAX_EVENTS = 1024


class DailyEventReviewError(ValueError):
    def __init__(self, code, path):
        self.code = code
        self.path = path
        super().__init__(code + ": " + path)


def _need(condition, code, path):
    if not condition:
        raise DailyEventReviewError(code, path)


def _closed(value, keys, path):
    _need(type(value) is dict and set(value) == set(keys), "invalid_schema", path)


def _text(value, path, *, limit=2000, nullable=False, identifier=False):
    if nullable and value is None:
        return
    _need(type(value) is str and 0 < len(value) <= limit and value.strip() == value and
          not any(ord(c) < 32 or ord(c) == 127 for c in value), "invalid_text", path)
    if identifier:
        _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", value) is not None,
              "invalid_identifier", path)


def _day(value, path):
    _need(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None,
          "invalid_date", path)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DailyEventReviewError("invalid_date", path) from None


def _utc(value, path):
    _need(type(value) is str and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value)
        is not None, "invalid_timestamp", path)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DailyEventReviewError("invalid_timestamp", path) from None
    _need(parsed.utcoffset() == timedelta(0), "invalid_timestamp", path)
    return parsed


def _list(value, path, maximum, *, minimum=0):
    _need(type(value) is list and minimum <= len(value) <= maximum,
          "invalid_list", path)


def _assets(value, path):
    _list(value, path, MAX_ASSETS, minimum=1)
    for i, asset in enumerate(value):
        _text(asset, f"{path}[{i}]", identifier=True)
    _need(len(set(value)) == len(value), "duplicate_asset", path)
    return sorted(value)


def _sessions(value, path):
    _list(value, path, MAX_SESSIONS, minimum=3)
    for i, day in enumerate(value):
        _day(day, f"{path}[{i}]")
    _need(value == sorted(set(value)), "invalid_sessions", path)
    return list(value)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _copy_json(value):
    stack = [(value, "ledger", 0)]
    count = 0
    while stack:
        item, path, depth = stack.pop()
        count += 1
        _need(depth <= 24 and count <= 25000, "json_complexity_limit", path)
        if type(item) is dict:
            _need(all(type(k) is str for k in item), "invalid_json_type", path)
            stack.extend((child, path + "." + key, depth + 1) for key, child in item.items())
        elif type(item) is list:
            stack.extend((child, f"{path}[{i}]", depth + 1) for i, child in enumerate(item))
        else:
            _need(item is None or type(item) in (str, int, bool), "invalid_json_type", path)
    try:
        serialized = _canonical(value)
        _need(len(serialized.encode("utf-8")) <= MAX_JSON_BYTES, "metadata_size_limit", "ledger")
        return json.loads(serialized)
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        if isinstance(exc, DailyEventReviewError):
            raise
        raise DailyEventReviewError("invalid_json_type", "ledger") from None


def _url(value, path):
    _text(value, path, limit=2000)
    _need(not any(c.isspace() for c in value) and "\\" not in value,
          "invalid_evidence_url", path)
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise DailyEventReviewError("invalid_evidence_url", path) from None
    _need(parsed.scheme == "https" and host is not None and
          re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host) is not None and
          "." in host and not host.endswith((".local", ".internal", ".localhost")) and
          not re.fullmatch(r"[0-9.]+", host) and port in (None, 443) and
          parsed.username is None and parsed.password is None and not parsed.fragment and
          (not parsed.query or value == "https://www.nasdaqtrader.com/trader.aspx?id=calendar"),
          "invalid_evidence_url", path)
    _need(all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
              is not None for label in host.split(".")) and
          re.fullmatch(r"[A-Za-z]{2,63}", host.split(".")[-1]) is not None and
          not ((host == "alphavantage.co" or host.endswith(".alphavantage.co")) and
               parsed.path.rstrip("/").casefold() == "/query"),
          "invalid_evidence_url", path)


def _refs(value, path, evidence, *, required, asset, event_type):
    _list(value, path, MAX_EVIDENCE, minimum=1 if required else 0)
    for i, reference in enumerate(value):
        _text(reference, f"{path}[{i}]", identifier=True)
        _need(reference in evidence, "unbound_evidence_reference", f"{path}[{i}]")
        applicability = evidence[reference]["applicability"]
        _need(asset in applicability["assets"] and event_type in applicability["event_types"],
              "evidence_applicability_mismatch", f"{path}[{i}]")
    _need(len(set(value)) == len(value), "duplicate_evidence_reference", path)


def parse_event_ledger(value, *, assets, sessions):
    """Validate closed declarations against an exact external asset/session scope.

    Return an independent JSON dictionary. No evidence truth or completeness is
    certified. Scope mismatch is rejected; date intersections are never taken.
    """
    external_assets = _assets(list(assets) if type(assets) is tuple else assets, "assets")
    external_sessions = _sessions(list(sessions) if type(sessions) is tuple else sessions, "sessions")
    d = _copy_json(value)
    _closed(d, ("schema_version", "ledger_version", "ledger_id", "created_at_utc",
                "selection", "evidence", "coverage", "events"), "ledger")
    _need(type(d["schema_version"]) is int and d["schema_version"] == 1 and
          d["ledger_version"] == VERSION, "unsupported_version", "ledger")
    _text(d["ledger_id"], "ledger.ledger_id", identifier=True)
    created = _utc(d["created_at_utc"], "ledger.created_at_utc")
    selection = d["selection"]
    _closed(selection, ("assets", "sessions"), "ledger.selection")
    declared_assets = _assets(selection["assets"], "ledger.selection.assets")
    declared_sessions = _sessions(selection["sessions"], "ledger.selection.sessions")
    _need(declared_assets == external_assets and declared_sessions == external_sessions,
          "selection_binding_mismatch", "ledger.selection")
    _need(selection["assets"] == declared_assets, "assets_not_canonical", "ledger.selection.assets")

    _list(d["evidence"], "ledger.evidence", MAX_EVIDENCE)
    evidence = {}
    for i, item in enumerate(d["evidence"]):
        path = f"ledger.evidence[{i}]"
        _closed(item, ("id", "url", "retrieved_at_utc", "published_date", "locator",
                       "excerpt", "statement", "applicability"), path)
        _text(item["id"], path + ".id", identifier=True)
        _need(item["id"] not in evidence, "duplicate_evidence_id", path + ".id")
        _url(item["url"], path + ".url")
        retrieved = _utc(item["retrieved_at_utc"], path + ".retrieved_at_utc")
        _need(retrieved <= created, "evidence_after_ledger_creation", path + ".retrieved_at_utc")
        if item["published_date"] is not None:
            _need(_day(item["published_date"], path + ".published_date") <= retrieved.date(),
                  "publication_after_retrieval", path + ".published_date")
        for field in ("locator", "excerpt", "statement"):
            _text(item[field], path + "." + field)
        applies = item["applicability"]
        _closed(applies, ("assets", "event_types"), path + ".applicability")
        applies_assets = _assets(applies["assets"], path + ".applicability.assets")
        _need(set(applies_assets) <= set(external_assets), "evidence_scope_mismatch", path)
        _list(applies["event_types"], path + ".applicability.event_types", len(EVENT_TYPES), minimum=1)
        for kind in applies["event_types"]:
            _need(type(kind) is str and kind in EVENT_TYPES, "evidence_scope_mismatch", path)
        _need(len(set(applies["event_types"])) == len(applies["event_types"]),
              "duplicate_evidence_event_type", path)
        evidence[item["id"]] = item

    _list(d["coverage"], "ledger.coverage", MAX_ASSETS * len(EVENT_TYPES))
    expected_pairs = {(asset, kind) for asset in external_assets for kind in EVENT_TYPES}
    actual_pairs = set()
    for i, item in enumerate(d["coverage"]):
        path = f"ledger.coverage[{i}]"
        _closed(item, ("asset", "event_type", "status", "statement", "evidence_refs"), path)
        _text(item["asset"], path + ".asset", identifier=True)
        _text(item["event_type"], path + ".event_type", identifier=True)
        pair = item["asset"], item["event_type"]
        _need(pair in expected_pairs, "coverage_scope_mismatch", path)
        _need(pair not in actual_pairs, "duplicate_coverage_pair", path)
        actual_pairs.add(pair)
        _need(item["status"] in ("unknown", "declared_complete"), "invalid_coverage_status", path)
        _text(item["statement"], path + ".statement", nullable=item["status"] == "unknown")
        _refs(item["evidence_refs"], path + ".evidence_refs", evidence,
              required=item["status"] == "declared_complete", asset=item["asset"],
              event_type=item["event_type"])
    _need(actual_pairs == expected_pairs, "coverage_denominator_incomplete", "ledger.coverage")

    _list(d["events"], "ledger.events", MAX_EVENTS)
    event_ids = set()
    for i, item in enumerate(d["events"]):
        path = f"ledger.events[{i}]"
        _closed(item, ("id", "asset", "event_type", "statement", "evidence_refs",
                       "effective_date", "other_dates"), path)
        _text(item["id"], path + ".id", identifier=True)
        _need(item["id"] not in event_ids, "duplicate_event_id", path + ".id")
        event_ids.add(item["id"])
        _text(item["asset"], path + ".asset", identifier=True)
        _text(item["event_type"], path + ".event_type", identifier=True)
        _need((item["asset"], item["event_type"]) in expected_pairs, "event_scope_mismatch", path)
        _text(item["statement"], path + ".statement")
        _refs(item["evidence_refs"], path + ".evidence_refs", evidence, required=True,
              asset=item["asset"], event_type=item["event_type"])
        effective = item["effective_date"]
        _closed(effective, ("status", "date", "basis", "evidence_refs"), path + ".effective_date")
        _need(effective["status"] in ("known", "unknown"), "invalid_effective_date_status", path)
        known = effective["status"] == "known"
        if known:
            _day(effective["date"], path + ".effective_date.date")
            _need(effective["basis"] == "explicit_effective_date", "unsupported_effective_date_basis", path)
        else:
            _need(effective["date"] is None and effective["basis"] == "unknown" and
                  effective["evidence_refs"] == [], "unknown_effective_date_has_value", path)
        _refs(effective["evidence_refs"], path + ".effective_date.evidence_refs", evidence, required=known,
              asset=item["asset"], event_type=item["event_type"])
        _need(set(effective["evidence_refs"]) <= set(item["evidence_refs"]),
              "effective_date_reference_not_bound_to_event", path)
        _list(item["other_dates"], path + ".other_dates", 3)
        other_kinds = set()
        for j, other in enumerate(item["other_dates"]):
            other_path = f"{path}.other_dates[{j}]"
            _closed(other, ("kind", "date", "evidence_refs"), other_path)
            _need(other["kind"] in ("announcement", "record", "payable") and
                  other["kind"] not in other_kinds, "invalid_other_date_kind", other_path)
            other_kinds.add(other["kind"])
            _day(other["date"], other_path + ".date")
            _refs(other["evidence_refs"], other_path + ".evidence_refs", evidence, required=True,
                  asset=item["asset"], event_type=item["event_type"])
            _need(set(other["evidence_refs"]) <= set(item["evidence_refs"]),
                  "other_date_reference_not_bound_to_event", other_path)
    return d


def assess_event_policy(value, *, assets, sessions, development_sessions, policy=POLICY):
    """Apply the existing rejection policy to a continuous development prefix.

    Unknown dates block rather than borrowing record/payable dates. Known final
    events are metadata only. Every return remains pending and non-executable.
    """
    _need(type(policy) is str and policy == POLICY, "unsupported_event_policy", "policy")
    d = parse_event_ledger(value, assets=assets, sessions=sessions)
    development = _sessions(list(development_sessions) if type(development_sessions) is tuple
                            else development_sessions, "development_sessions")
    all_sessions = d["selection"]["sessions"]
    _need(len(development) <= len(all_sessions) and
          development == all_sessions[:len(development)], "development_prefix_required", "development_sessions")
    blockers = []

    def block(code, item, event_id=None):
        blockers.append({"code": code, "asset": item["asset"],
                         "event_type": item["event_type"], "event_id": event_id})

    unknown_coverage = [c for c in d["coverage"] if c["status"] == "unknown"]
    for coverage in unknown_coverage:
        block("unknown_event_coverage", coverage)
    known, unknown, unsupported = [], [], []
    for event in d["events"]:
        if event["effective_date"]["status"] == "unknown":
            unknown.append(event)
            block("unknown_effective_date", event, event["id"])
            continue
        effective = event["effective_date"]["date"]
        if all_sessions[0] <= effective <= development[-1]:
            location = "development"
        elif development[-1] < effective <= all_sessions[-1]:
            location = "final_metadata_only"
        else:
            location = "outside_selection"
        annotated = {**event, "location": location}
        known.append(annotated)
        if location == "development":
            unsupported.append(annotated)
            block("unsupported_development_event", event, event["id"])
    return {
        "schema_version": 1, "review_version": VERSION, "ledger_id": d["ledger_id"],
        "canonical_ledger_digest": hashlib.sha256(_canonical(d).encode("utf-8")).hexdigest(),
        "selection": d["selection"], "policy": policy,
        "status": "blocked" if blockers else "policy_clear_pending_authority",
        "coverage_summary": {"expected_pairs": len(d["coverage"]),
                             "unknown_pairs": len(unknown_coverage),
                             "declared_complete_pairs": len(d["coverage"]) - len(unknown_coverage),
                             "declared_complete_is_authenticated": False},
        "coverage": d["coverage"], "known_events": known, "unknown_events": unknown,
        "unsupported_development_events": unsupported, "development_sessions": development,
        "blockers": blockers, "source_admission": "pending", "execution_allowed": False,
        "independent_authentication": "pending", "numeric_market_rows_parsed": 0,
        "final_numeric_rows_parsed": 0,
    }
