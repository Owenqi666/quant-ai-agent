"""Metadata preparation and unverified review submissions, never real authority.

No API here accepts market panels, parses CSV or registers a dataset. Numeric
adaptation is tested separately with newly generated engineering fixtures.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .daily_contract import MAX_METADATA_BYTES, MAX_RAW_BYTES, parse_contract, require_fields
from .daily_source_onboarding_v1 import (
    FACTS, _descriptor, _info, _json, _path, _read, inspect_source_bundle,
)
from .expressions import validate_expression
from .server.provider_catalog import catalog_from_daily_contract
from .storage import digest

VERSION = "daily-real-preparation-v1"
REVIEW_VERSION = "daily-real-review-submission-v1"
PLAN_VERSION = "daily-real-development-adaptation-plan-v1"
EXPRESSION_101 = "((close - open) / ((high - low) + .001))"
DETAIL_KEYS = ("provider", "source_version", "retrieved_at_utc")
SELECTION_KEYS = ("exchange", "timezone", "assets", "start_date", "end_date", "selected_as_of_utc")
FACT_REQUIREMENTS = {
    "provider": "Retain the supplier/feed/version and original field documentation with acquisition time.",
    "rights": "Retain applicable terms and assess the exact intended local use, storage and redistribution scope.",
    "calendar": "Retain independent venue sessions/closures/half-days and IANA timezone for the selected dates; never infer sessions from observed rows.",
    "units": "Document raw unadjusted OHLC in major currency at scale1 and unadjusted share volume; no adjusted-close or VWAP proxy.",
    "universe": "Document exact assets, one venue/currency and selection timestamp, with static-current-vintage/survivorship limitations.",
    "events": "Document listing/delisting/halt/no-quote/split/dividend coverage; unsupported development events remain blocking.",
    "vintage": "Retain acquisition/adjustment/universe vintage; a current snapshot never establishes point-in-time availability.",
    "raw_identity": "Retain actual raw bytes, SHA256 and size plus explicit date/asset/OHLCV mapping; absent raw identity stays null.",
}
CODE_FILES = (
    "paper_alpha/daily_real_preparation.py", "scripts/prepare_daily_real_source.py",
    "paper_alpha/daily_source_onboarding_v1.py", "paper_alpha/daily_adapter.py",
    "paper_alpha/daily_contract.py", "paper_alpha/server/daily_source_qualification_v2.py",
    "paper_alpha/server/provider_catalog.py", "paper_alpha/server/provider_field_semantics.py",
    "paper_alpha/evaluation.py", "paper_alpha/expressions.py", "paper_alpha/vendor/operators.py",
    "paper_alpha/storage.py", "paper_alpha/market_diagnostics.py",
)


class DailyRealPreparationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _need(condition, code, message):
    if not condition:
        raise DailyRealPreparationError(code, message)


def _closed(value, keys, label):
    _need(type(value) is dict and set(value) == set(keys), "invalid_schema", "Closed " + label + " required")


def _text(value, *, nullable=False, identifier=False):
    if nullable and value is None:
        return
    _need(type(value) is str and 0 < len(value) <= 4000 and value.strip() == value,
          "invalid_text", "Bounded nonempty text required")
    if identifier:
        _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", value) is not None,
              "invalid_identifier", "Bounded identifier required")


def _sha(value):
    _need(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
          "invalid_digest", "Lower-case SHA256 required")


def _load(path, *, expected_sha256=None, raw_path=None, raw_sha256=None):
    # A caller cannot use the raw role as preparation/request/review metadata.
    raw_inode = None
    if raw_path is not None:
        raw_info = _info(raw_path, "raw", MAX_RAW_BYTES)
        raw_inode = (raw_info.st_dev, raw_info.st_ino)
        input_info = _info(Path(path).absolute(), "preparation_metadata", MAX_METADATA_BYTES)
        _need((input_info.st_dev, input_info.st_ino) != raw_inode,
              "raw_alias", "Raw role cannot be opened as preparation metadata")
    payload, identity, _ = _read(Path(path).absolute(), "preparation_metadata", MAX_METADATA_BYTES,
                               raw_inode=raw_inode)
    if expected_sha256 is not None:
        _sha(expected_sha256)
        _need(identity["sha256"] == expected_sha256, "original_pin_mismatch", "Original metadata bytes differ")
    if raw_sha256 is not None:
        _need(identity["sha256"] != raw_sha256, "raw_alias", "Copied raw content cannot be metadata")
    return _json(payload, "preparation_metadata"), identity


def _code():
    root = Path(__file__).resolve().parents[1]
    files = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in CODE_FILES}
    return {"files": files, "digest": digest(files)}


def _boundary():
    return {"real_source_ready": False, "execution_allowed": False,
            "real_execution_status": "BLOCKED_EXTERNAL",
            "independent_authentication": "pending", "numeric_market_rows_parsed": 0,
            "final_numeric_rows_parsed": 0, "model_calls": 0, "db_mutations": 0}


def source_preparation_template():
    """No made-up supplier identity, hashes, raw size or calendar sessions."""
    return {"schema_version": 1, "preparation_version": VERSION, "source_id": None,
            "intended_use": None, "source_details": dict.fromkeys(DETAIL_KEYS),
            "selection": dict.fromkeys(SELECTION_KEYS), "raw": None,
            "facts": {fact: {"status": "unknown", "statement": None, "evidence_sha256": None}
                      for fact in FACTS}, "pit": "unknown"}


def _validate_draft(value):
    _closed(value, source_preparation_template(), "preparation draft")
    _need(type(value["schema_version"]) is int and value["schema_version"] == 1 and
          value["preparation_version"] == VERSION, "unsupported_version", "Unsupported preparation version")
    _text(value["source_id"], nullable=True, identifier=True)
    _text(value["intended_use"], nullable=True)
    _closed(value["source_details"], DETAIL_KEYS, "source details")
    for item in value["source_details"].values():
        _text(item, nullable=True)
    _closed(value["selection"], SELECTION_KEYS, "selection")
    for key in ("exchange", "timezone", "start_date", "end_date", "selected_as_of_utc"):
        _text(value["selection"][key], nullable=True, identifier=key == "exchange")
    for timestamp in (value["source_details"]["retrieved_at_utc"], value["selection"]["selected_as_of_utc"]):
        if timestamp is not None:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            _need(parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed),
                  "invalid_timestamp", "Acquisition and selection timestamps must include UTC offset")
    selection = value["selection"]
    for key in ("start_date", "end_date"):
        if selection[key] is not None:
            _need(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", selection[key])), "invalid_date", "ISO date required")
            date.fromisoformat(selection[key])
    if selection["start_date"] is not None and selection["end_date"] is not None:
        _need(selection["start_date"] <= selection["end_date"], "invalid_date", "Selection range is reversed")
    if selection["timezone"] is not None:
        ZoneInfo(selection["timezone"])
    if selection["assets"] is not None:
        assets = selection["assets"]
        _need(type(assets) is list and 2 <= len(assets) <= 10000, "invalid_universe", "Bounded asset list required")
        for asset in assets:
            _text(asset, identifier=True)
        _need(len(set(assets)) == len(assets), "invalid_universe", "Unique assets required")
    if value["raw"] is not None:
        _descriptor(value["raw"], "raw", MAX_RAW_BYTES)
        # Validate spelling without opening or requiring a yet-absent raw file.
        relative = value["raw"]["relative_path"]
        _need(type(relative) is str and len(relative) <= 1000 and "\\" not in relative and
              "\x00" not in relative and not relative.startswith("/") and
              all(p not in ("", ".", "..") for p in relative.split("/")), "unsafe_path", "Bundle-relative raw path required")
    _facts(value["facts"])
    _need(value["pit"] in ("unknown", "not_certified", "certification_evidence_pending"),
          "pit_self_certification", "Preparation cannot certify PIT")
    return value


def _facts(value):
    _closed(value, FACTS, "eight-fact denominator")
    for item in value.values():
        _closed(item, ("status", "statement", "evidence_sha256"), "source fact")
        _need(item["status"] in ("unknown", "supported", "rejected"), "invalid_fact_status", "Unknown fact status")
        _text(item["statement"], nullable=True)
        if item["evidence_sha256"] is not None:
            _sha(item["evidence_sha256"])
        if item["status"] != "unknown":
            _need(item["statement"] is not None and item["evidence_sha256"] is not None,
                  "missing_evidence", "Decisions require a reason and retained evidence pin")


def _bundle(bundle_path, allow_whole_byte_hash):
    _need(type(allow_whole_byte_hash) is bool, "invalid_permission", "Explicit boolean byte permission required")
    if bundle_path is None:
        _need(not allow_whole_byte_hash, "missing_bundle", "Whole-byte permission needs an actual retained bundle")
        return None, None, {}
    report = inspect_source_bundle(bundle_path, allow_whole_byte_hash=allow_whole_byte_hash)
    bundle, _ = _load(bundle_path, expected_sha256=report["bundle_identity"]["sha256"])
    root = Path(bundle_path).absolute().parent
    raw_path = _path(root, bundle["raw"]["relative_path"], "raw")
    return report, bundle, {"raw_path": raw_path, "raw_sha256": bundle["raw"]["sha256"]}


def _assess(preparation_path, *, bundle_path=None, allow_whole_byte_hash=False):
    onboarding, bundle, guard = _bundle(bundle_path, allow_whole_byte_hash)
    draft, identity = _load(preparation_path, **guard)
    draft = _validate_draft(draft)
    missing = ["draft:" + key for key in ("source_id", "intended_use", "raw") if draft[key] is None]
    missing += ["draft:source_details:" + k for k, v in draft["source_details"].items() if v is None]
    missing += ["draft:selection:" + k for k, v in draft["selection"].items() if v is None]
    missing += ["draft:fact:" + fact for fact in FACTS if draft["facts"][fact]["status"] != "supported"]
    if onboarding is None:
        missing += ["actual_retained_bundle"] + ["retained_evidence:" + fact for fact in FACTS]
    else:
        missing += onboarding["missing_inputs"]
        if draft["source_id"] is not None:
            _need(draft["source_id"] == onboarding["source_id"], "source_mismatch", "Preparation source differs from original bundle")
        if draft["raw"] is not None:
            _need(draft["raw"] == bundle["raw"], "raw_binding_mismatch", "Preparation raw descriptor differs from original bundle")
        documents = {}
        for role in ("contract", "receipt", "review"):
            documents[role], _ = _load(_path(Path(bundle_path).absolute().parent, bundle[role]["relative_path"], role),
                                     expected_sha256=bundle[role]["sha256"], **guard)
        contract, receipt = documents["contract"], documents["receipt"]
        for k, v in draft["source_details"].items():
            if v is not None:
                _need(v == receipt[k], "source_mismatch", "Source details differ from retained receipt")
        actual_selection = {"exchange": contract["calendar"]["exchange"], "timezone": contract["calendar"]["timezone"],
                            "assets": sorted(a["id"] for a in contract["universe"]["assets"]),
                            "start_date": contract["calendar"]["sessions"][0], "end_date": contract["calendar"]["sessions"][-1],
                            "selected_as_of_utc": contract["universe"]["selected_as_of_utc"]}
        for k, v in draft["selection"].items():
            if v is not None:
                _need((sorted(v) if k == "assets" else v) == actual_selection[k],
                      "selection_mismatch", "Selection differs from retained contract")
        for fact, item in draft["facts"].items():
            if item["status"] != "unknown":
                _need(item == documents["review"]["facts"][fact], "fact_binding_mismatch", "Draft decisions differ from retained review")
    value = {"schema_version": 1, "preparation_version": VERSION,
             "status": "needs_inputs" if missing else "prepared_for_external_review",
             "preparation_identity": identity, "source_id": draft["source_id"],
             "requirements": deepcopy(FACT_REQUIREMENTS), "missing_inputs": sorted(set(missing)),
             "independent_missing_inputs": ["trusted_independent_source_and_rights_assessment", "approved_real_registration_and_worker_integration"],
             "onboarding": onboarding, "review_submitted": False, "point_in_time": draft["pit"],
             "whole_byte_integrity_permission": allow_whole_byte_hash, "code": _code(), **_boundary()}
    _load(preparation_path, expected_sha256=identity["sha256"], **guard)
    if onboarding is not None:
        _need(_bundle(bundle_path, allow_whole_byte_hash)[0] == onboarding,
              "input_changed", "Bundle changed during preparation")
    value["report_digest"] = digest(value)
    return value, guard


def assess_preparation(preparation_path, *, bundle_path=None, allow_whole_byte_hash=False):
    return _assess(preparation_path, bundle_path=bundle_path, allow_whole_byte_hash=allow_whole_byte_hash)[0]


def create_independent_review_request(preparation_path, *, bundle_path=None, allow_whole_byte_hash=False):
    assessment = assess_preparation(preparation_path, bundle_path=bundle_path,
                                    allow_whole_byte_hash=allow_whole_byte_hash)
    value = {"schema_version": 1, "request_version": VERSION, "source_id": assessment["source_id"],
             "assessment": assessment, "required_facts": list(FACTS), "review_submitted": False, **_boundary()}
    value["request_digest"] = digest(value)
    return value


def inspect_independent_review(request_path, review_path, *, expected_request_sha256,
                               preparation_path, bundle_path=None, allow_whole_byte_hash=False):
    current, guard = _assess(preparation_path, bundle_path=bundle_path,
                                allow_whole_byte_hash=allow_whole_byte_hash)
    request, request_identity = _load(request_path, expected_sha256=expected_request_sha256, **guard)
    expected = {"schema_version": 1, "request_version": VERSION, "source_id": current["source_id"],
                "assessment": current, "required_facts": list(FACTS), "review_submitted": False, **_boundary()}
    expected["request_digest"] = digest(expected)
    _need(request == expected, "request_binding_mismatch", "Request differs from current original input/code identities")
    review, review_identity = _load(review_path, **guard)
    _need(review_identity != request_identity and review_identity != current["preparation_identity"],
          "review_alias", "Review must be a separate submission")
    _closed(review, ("schema_version", "review_version", "request_sha256", "source_id", "reviewer", "facts", "pit", "limitations"), "independent review submission")
    _need(type(review["schema_version"]) is int and review["schema_version"] == 1 and review["review_version"] == REVIEW_VERSION,
          "unsupported_version", "Unsupported review submission")
    _need(review["request_sha256"] == expected_request_sha256 and review["source_id"] == request["source_id"],
          "review_binding_mismatch", "Review source or original request pin differs")
    _closed(review["reviewer"], ("id", "kind"), "reviewer identity")
    _text(review["reviewer"]["id"])
    _need(review["reviewer"]["kind"] in ("human", "automation"), "invalid_reviewer", "Declared reviewer kind required")
    _facts(review["facts"])
    _need(review["pit"] in ("unknown", "not_certified", "certification_evidence_pending"),
          "pit_self_certification", "Submission cannot certify PIT")
    _need(type(review["limitations"]) is list and 1 <= len(review["limitations"]) <= 100,
          "missing_limitations", "Explicit review limitations required")
    for item in review["limitations"]:
        _text(item)
    for fact, item in review["facts"].items():
        if item["status"] != "unknown":
            evidence = current["onboarding"]["fact_evidence"][fact] if current["onboarding"] else None
            _need(evidence is not None and evidence["retained"] and item["evidence_sha256"] == evidence["identity"]["sha256"],
                  "review_evidence_mismatch", "Review decisions require the exact original retained evidence")
    result = {"schema_version": 1, "review_version": REVIEW_VERSION, "status": "review_submitted",
              "review_submitted": True, "reviewer_identity_authenticated": False,
              "request_identity": request_identity, "review_identity": review_identity,
              "assessment_digest": current["report_digest"], "reviewer": deepcopy(review["reviewer"]),
              "decisions": deepcopy(review["facts"]), "point_in_time": review["pit"],
              "missing_inputs": current["missing_inputs"],
              "limitations": deepcopy(review["limitations"]) + ["A submitted assessment does not authenticate reviewer identity, source rights or PIT and grants no execution."],
              **_boundary()}
    _load(request_path, expected_sha256=request_identity["sha256"], **guard)
    _load(review_path, expected_sha256=review_identity["sha256"], **guard)
    _need(assess_preparation(preparation_path, bundle_path=bundle_path,
                            allow_whole_byte_hash=allow_whole_byte_hash) == current,
          "input_changed", "Original review inputs changed")
    result["report_digest"] = digest(result)
    return result


def prepare_development_plan(contract_path, *, expected_contract_sha256, expression=EXPRESSION_101,
                             required_fields=("open", "high", "low", "close")):
    raw, identity = _load(contract_path, expected_sha256=expected_contract_sha256)
    contract = parse_contract(raw)
    required = require_fields(contract, required_fields)
    checked = validate_expression(expression, {"open", "high", "low", "close", "volume", "returns"})
    _need(set(checked["used_fields"]) <= set(required), "undeclared_field", "Every used field must be declared")
    def lookback(node):
        if isinstance(node, ast.Name):
            return 1 if node.id == "returns" else 0
        if isinstance(node, ast.Call) and node.func.id in {"delay", "ts_delta", "ts_corr", "ts_mean", "ts_std", "ts_min", "ts_max", "ts_sum"}:
            window = node.args[-1].value
            return max(lookback(arg) for arg in node.args[:-1]) + (window if node.func.id in {"delay", "ts_delta"} else window - 1)
        return max((lookback(child) for child in ast.iter_child_nodes(node)), default=0)
    warmup = lookback(ast.parse(checked["canonical_expression"], mode="eval").body)
    dates = contract.development_calendar
    config = contract.to_dict()["evaluation"]
    start = dates.index(config["splits"]["validation"]["start"])
    stop = len(dates) - 2
    eligible = dates[max(start, warmup):stop]
    blocked = []
    if len(contract.assets) < config["min_assets"]:
        blocked.append("insufficient_assets")
    if not eligible:
        blocked.append("no_validation_rows_after_warmup_and_purge")
    if any(event["date"] <= contract.cutoff for event in contract.to_dict()["known_events"]):
        blocked.append("unsupported_development_event")
    catalog = catalog_from_daily_contract(contract)
    value = {"schema_version": 1, "plan_version": PLAN_VERSION, "status": "adaptation_blocked" if blocked else "adaptation_planned",
             "scope": "metadata_only_non_executable_development_plan", "is_execution_descriptor": False,
             "contract_identity": identity, "contract_digest": contract.contract_digest,
             "declared_origin": contract.to_dict()["origin"], "catalog_digest": catalog["catalog_digest"],
             "calendar_digest": digest(list(contract.calendar)), "config_digest": digest(config),
             "development_sessions": list(dates), "assets": list(contract.assets),
             "development": deepcopy(catalog["public_projection"]["declared_capabilities"]["development"]),
             "execution_semantics": deepcopy(catalog["public_projection"]["declared_capabilities"]["execution"]),
             "expression": expression, "expression_validation": checked, "required_fields": list(required),
             "warmup_rows": warmup, "eligible_signal_sessions": list(eligible), "purged_validation_sessions": list(dates[-2:]),
             "static_blockers": blocked, "numeric_validation": "not_performed",
             "independent_missing_inputs": ["actual_source_and_rights_authentication", "registered_real_source_authority", "current_file_backed_prefix_precheck"],
             "code": _code(), **_boundary()}
    _load(contract_path, expected_sha256=identity["sha256"])
    value["plan_digest"] = digest(value)
    return value
