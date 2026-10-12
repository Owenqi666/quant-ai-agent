"""Pure main-lane assessment metadata; no filesystem or current-code reads.

Declared original paths are lexical metadata. The permitted normalization gate
must verify physical files independently. This does not grant source authority.
The legacy B1/Host physical-path parser is deliberately left unchanged.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .real_daily_admission import FACTS, MAX_METADATA, MAX_RAW, VERSION
from .real_daily_web_contract import parse_assessment_request
from .real_daily_method import assess_events, method_profile
from .storage import digest, json_text

_SHA = re.compile(r"[0-9a-f]{64}\Z")
ASSESSMENT_KEYS = frozenset({
    "schema_version", "assessment_version", "selection", "ledger", "cash_annotations",
    "source_facts", "input_pins", "method", "method_digest", "event_review", "code_identity",
    "blockers", "status", "origin", "source_authenticated", "real_source_ready", "point_in_time",
    "local_development_execution_allowed", "final_numeric_access_allowed", "final_numeric_rows_parsed",
    "limitations", "assessment_digest",
})
LIMITATIONS = [
    "OHLC-only raw-price method; volume units are not authenticated.",
    "Source evidence completeness is a declaration, not provider or rights authentication.",
    "Static current-vintage universe and historical PIT not certified.",
    "Cash dividends, costs, slippage and borrow are excluded from returns.",
]


class RealDailyMainMetadataError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def need(condition, code):
    if not condition:
        raise RealDailyMainMetadataError(code)


def snapshot(value, *, maximum=MAX_METADATA):
    """Copy exact JSON builtins with bounded depth, population and UTF8 size."""
    population = 0

    def clone(item, depth=0):
        nonlocal population
        population += 1
        need(population <= 100_000 and depth <= 64, "bounded_metadata_required")
        if type(item) is dict:
            need(len(item) <= 100_000, "bounded_metadata_required")
            result = {}
            for key, child in tuple(item.items()):
                need(type(key) is str and len(key) <= 256, "invalid_metadata")
                result[key] = clone(child, depth + 1)
            return result
        if type(item) is list:
            need(len(item) <= 100_000, "bounded_metadata_required")
            return [clone(child, depth + 1) for child in tuple(item)]
        need(type(item) in (str, int, float, bool) or item is None, "invalid_metadata")
        if type(item) is str:
            need(len(item) <= 65_536, "bounded_metadata_required")
        if type(item) is int:
            need(abs(item) <= 2**63 - 1, "bounded_metadata_required")
        return item

    try:
        copied = clone(value)
        need(len(json_text(copied).encode("utf-8")) <= maximum, "bounded_metadata_required")
        return copied
    except RealDailyMainMetadataError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, RuntimeError):
        raise RealDailyMainMetadataError("invalid_metadata") from None


def pin(value):
    need(type(value) is str and _SHA.fullmatch(value) is not None, "invalid_pin")
    return value


def path_literal(value):
    """Validate declarations only; never resolve/stat/open any path component."""
    need(type(value) is str and value.startswith("/") and len(value) <= 4096 and value != "/",
         "absolute_path_required")
    components = value[1:].split("/")
    need(all(component and component not in (".", "..") and "\\" not in component and
             not any(ord(c) < 32 or ord(c) == 127 for c in component) for component in components),
         "unsafe_path")
    need(not any(component.casefold() in ("secrets", "credentials", "alpha-vantage-claims") or
                 component.casefold().endswith(".key") for component in components), "secret_path_refused")
    return value


def validate_pin_descriptors(value, assets):
    need(type(value) is dict and set(value) == {"origin", "metadata", "raw", "code"}, "invalid_input_pins")
    need(value["origin"] in ("engineering_fixture", "real_source_declaration"), "invalid_origin")
    need(type(value["metadata"]) is list and 1 <= len(value["metadata"]) <= 50 and
         type(value["code"]) is list and len(value["code"]) <= 50 and
         type(value["raw"]) is list and len(value["raw"]) == len(assets), "invalid_input_pins")
    seen = set()
    for role in ("metadata", "raw", "code"):
        for descriptor in value[role]:
            keys = {"asset", "path", "sha256", "size_bytes"} if role == "raw" else {"path", "sha256"}
            need(type(descriptor) is dict and set(descriptor) == keys, "invalid_input_pin")
            name = path_literal(descriptor["path"])
            pin(descriptor["sha256"])
            need(name not in seen, "input_role_alias")
            seen.add(name)
            if role == "raw":
                need(descriptor["asset"] in assets and type(descriptor["size_bytes"]) is int and
                     0 < descriptor["size_bytes"] <= MAX_RAW, "invalid_raw_descriptor")
            else:
                stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold()
                need(not name.casefold().endswith(".raw.json") and "raw" not in stem, "metadata_role_required")
    raw_pins = {descriptor["sha256"] for descriptor in value["raw"]}
    need(not any(descriptor["sha256"] in raw_pins for descriptor in value["metadata"] + value["code"]),
         "copied_raw_role_alias")
    need(sorted(descriptor["asset"] for descriptor in value["raw"]) == assets, "raw_asset_mismatch")
    return deepcopy(value)


def parse_main_assessment_request(value):
    parsed = parse_assessment_request(snapshot(value))
    parsed["input_pins"] = validate_pin_descriptors(parsed["input_pins"], parsed["selection"]["assets"])
    return parsed


def validate_code_identity(value):
    value = snapshot(value, maximum=64 * 1024)
    need(type(value) is dict and set(value) == {"version", "files", "digest"} and value["version"] == VERSION and
         type(value["files"]) is dict and 0 < len(value["files"]) <= 50, "invalid_code_identity")
    for name, sha in value["files"].items():
        need(type(name) is str and name.startswith("paper_alpha/") and len(name) <= 256 and
             all(component and component not in (".", "..") for component in name.split("/")),
             "invalid_code_identity")
        pin(sha)
    need(value["digest"] == digest(value["files"]), "invalid_code_identity")
    return value


def build_main_assessment(request, *, code_identity):
    """Same closed frozen assessment semantics, without original-path stat."""
    parsed = parse_main_assessment_request(request)
    code = validate_code_identity(code_identity)
    pins = parsed["input_pins"]
    evidence = {item["sha256"] for item in pins["metadata"] + pins["raw"]}
    blockers = []
    for name in FACTS:
        fact = parsed["source_facts"][name]
        if fact["statement"] is not None:
            need(fact["statement"].strip() == fact["statement"] and bool(fact["statement"]), "invalid_source_fact")
        if fact["status"] == "supported":
            need(fact["statement"] is not None and fact["evidence_sha256"] in evidence, "unbound_source_evidence")
        else:
            blockers.append({"code": "source_fact_" + fact["status"], "fact": name})
    method = method_profile()
    events = assess_events(parsed["ledger"], selection=parsed["selection"], cash_annotations=parsed["cash_annotations"])
    blockers.extend(deepcopy(events["blockers"]))
    value = {
        "schema_version": 1, "assessment_version": VERSION, "selection": parsed["selection"],
        "ledger": parsed["ledger"], "cash_annotations": parsed["cash_annotations"],
        "source_facts": parsed["source_facts"], "input_pins": pins, "method": method,
        "method_digest": digest(method), "event_review": events, "code_identity": code, "blockers": blockers,
        "status": "blocked" if blockers else "ready_for_host_review", "origin": pins["origin"],
        "source_authenticated": False, "real_source_ready": False, "point_in_time": "not_certified",
        "local_development_execution_allowed": False, "final_numeric_access_allowed": False,
        "final_numeric_rows_parsed": 0, "limitations": deepcopy(LIMITATIONS),
    }
    value["assessment_digest"] = digest(value)
    return value


def validate_main_assessment(value):
    """Frozen semantic read, independent of current files/code/permissions."""
    saved = snapshot(value)
    need(type(saved) is dict and set(saved) == ASSESSMENT_KEYS, "invalid_assessment_schema")
    request = {"schema_version": 1, **{key: saved[key] for key in
        ("selection", "ledger", "cash_annotations", "source_facts", "input_pins", "method_digest")}}
    expected = build_main_assessment(request, code_identity=saved["code_identity"])
    need(json_text(saved) == json_text(expected), "assessment_metadata_drift")
    return saved
