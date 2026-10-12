"""Closed metadata, four-field catalogs and host-produced development bindings.

These helpers never open raw market files, keys, a business database or a
scheduler. ``assessment_from_request`` reuses the existing admission builder,
which reads its restricted tracked-code identity; it is not zero-filesystem-I/O.
Input pin paths are internal host descriptors after opaque import resolution,
not a web API permission to open arbitrary request paths. No helper grants
execution, authenticates a source or unlocks final-period numeric values.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .daily_event_review import DailyEventReviewError, parse_event_ledger
from .evaluation import AVAILABILITY, EXECUTION
from .expressions import MAX_WINDOW, OPERATOR_SEMANTICS_VERSION, _ALIASES, _FUNCTION_ARITY
from .real_daily_admission import FACTS, MAX_METADATA, MAX_RAW, VERSION as ADMISSION_VERSION, RealDailyAdmissionError, _pins, _selection, build_assessment
from .real_daily_method import RealDailyMethodError, assess_events, method_profile
from .storage import digest, json_text

TASK_KIND = "real_daily_ohlc_development_v1"
DATA_KIND = "real_daily_ohlc_development"
CATALOG_ID = "real-daily-ohlc-web-catalog-v1"
CAPABILITY_ID = "real-daily-ohlc-development-capabilities-v1"
MAX_CATALOG_BYTES = 256 * 1024
FIELDS = ("open", "high", "low", "close")
FINAL_POLICY = "Final-test data and evaluation are unavailable."
LIMITATIONS = [
    "Four-field raw OHLC declaration only; volume, returns and VWAP are unavailable, without proxies.",
    "Source completeness and local approval do not authenticate provider, rights or historical PIT.",
    "Static current-vintage universe; no survivorship or point-in-time certification.",
    "Returns are gross raw price returns; cash dividends, costs, slippage and borrow are excluded.",
    "Validation-only development; reserved final values and evaluation are unavailable.",
    "Small asset cross sections demonstrate software behavior, not economic or statistical validity.",
]
EXECUTION_KEYS = frozenset({
    "schema_version", "kind", "data_kind", "scope", "origin", "dataset_id", "registration_id",
    "assessment_id", "registration_digest", "assessment_digest", "method_digest", "selection_digest",
    "catalog_digest", "prefix_sha256", "config_digest", "source_digest",
})
REGISTRATION_KEYS = frozenset({
    "schema_version", "registration_version", "id", "approval_id", "approval_digest", "assessment_id",
    "assessment_digest", "reservation_id", "reservation_digest", "prefix_path", "prefix_sha256",
    "receipt_path", "receipt_sha256", "selection_json", "method_digest", "code_identity_json", "origin",
    "status", "final_numeric_access_allowed", "source_authenticated", "real_source_ready",
})

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
Identity = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Asset = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")]
Day = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Origin = Literal["engineering_fixture", "real_source_declaration"]


class RealDailyWebContractError(ValueError):
    """Bounded error code; never embeds private paths or metadata text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise RealDailyWebContractError(code)


class MetadataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SourceFact(MetadataModel):
    status: Literal["supported", "unknown", "rejected"]
    statement: Annotated[str, StringConstraints(min_length=1, max_length=4000)] | None
    evidence_sha256: Sha256 | None


class SourceFacts(MetadataModel):
    provider: SourceFact
    rights: SourceFact
    calendar: SourceFact
    units: SourceFact
    universe: SourceFact
    events: SourceFact
    vintage: SourceFact
    raw_identity: SourceFact


class DateBounds(MetadataModel):
    start: Day
    end: Day


class Splits(MetadataModel):
    train: DateBounds
    validation: DateBounds
    test: DateBounds


class EvaluationConfig(MetadataModel):
    splits: Splits
    min_assets: int = Field(ge=3, le=100)


class Partitions(MetadataModel):
    train: list[Day] = Field(min_length=3, max_length=1024)
    validation: list[Day] = Field(min_length=3, max_length=1024)
    test: list[Day] = Field(min_length=3, max_length=1024)


class Selection(MetadataModel):
    assets: list[Asset] = Field(min_length=3, max_length=100)
    sessions: list[Day] = Field(min_length=9, max_length=1024)
    development_sessions: list[Day] = Field(min_length=6, max_length=1024)
    config: EvaluationConfig
    partitions: Partitions
    # Optional legacy metadata is retained only when submitted. Literal defaults
    # allow omitted fields; all serializers use exclude_unset=True so neither
    # default can change the original selection payload or its digest.
    configuration_origin: Literal["project_configuration", "project_engineering_fixture"] = "project_configuration"
    point_in_time: Literal["not_certified"] = "not_certified"


class MetadataPin(MetadataModel):
    path: str = Field(min_length=1, max_length=4000)
    sha256: Sha256


class RawDescriptor(MetadataPin):
    asset: Asset
    size_bytes: int = Field(ge=1, le=MAX_RAW)


class InputPins(MetadataModel):
    origin: Origin
    metadata: list[MetadataPin] = Field(min_length=1, max_length=50)
    raw: list[RawDescriptor] = Field(min_length=3, max_length=100)
    code: list[MetadataPin] = Field(max_length=50)


class CashAnnotation(MetadataModel):
    event_id: Asset
    subtype: Literal["cash_dividend"]
    statement: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[Asset] = Field(min_length=1, max_length=512)


class AssessmentRequest(MetadataModel):
    """Internal metadata contract, after the host has resolved import handles."""
    schema_version: int = Field(ge=1, le=1)
    selection: Selection
    ledger: dict
    cash_annotations: list[CashAnnotation] = Field(max_length=1024)
    source_facts: SourceFacts
    input_pins: InputPins
    method_digest: Sha256


class MethodMetadata(MetadataModel):
    method_id: Literal["daily-raw-price-ohlc-method-v1"]
    method_digest: Sha256
    profile: dict


class ExecutionBinding(MetadataModel):
    schema_version: int = Field(ge=1, le=1)
    kind: Literal["real_daily_ohlc_development_v1"]
    data_kind: Literal["real_daily_ohlc_development"]
    scope: Literal["development_only"]
    origin: Origin
    dataset_id: Identity
    registration_id: Identity
    assessment_id: Identity
    registration_digest: Sha256
    assessment_digest: Sha256
    method_digest: Sha256
    selection_digest: Sha256
    catalog_digest: Sha256
    prefix_sha256: Sha256
    config_digest: Sha256
    source_digest: Sha256


def _finite_bound(value, limit=MAX_METADATA):
    try:
        _need(len(json_text(value).encode("utf-8")) <= limit, "metadata_size_limit")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        if isinstance(exc, RealDailyWebContractError):
            raise
        raise RealDailyWebContractError("invalid_metadata_json") from None


def validate_selection(value):
    """Closed full date/asset scope, without market reads or configuration changes."""
    try:
        parsed = Selection.model_validate(value).model_dump(exclude_unset=True)
        return _selection(parsed)
    except (ValidationError, RealDailyAdmissionError, ValueError) as exc:
        if isinstance(exc, RealDailyAdmissionError):
            raise RealDailyWebContractError(exc.code) from None
        raise RealDailyWebContractError("invalid_selection") from None


def method_metadata():
    p = method_profile()
    return {"method_id": p["method_id"], "method_digest": digest(p), "profile": p}


def validate_method_metadata(value):
    try:
        parsed = MethodMetadata.model_validate(value).model_dump()
    except ValidationError:
        raise RealDailyWebContractError("invalid_method_metadata") from None
    _need(json_text(parsed) == json_text(method_metadata()), "method_drift")
    return parsed


def parse_assessment_request(value):
    """Validate bounded metadata only; does not rebuild current code identity."""
    _finite_bound(value)
    if type(value) is dict and type(value.get("source_facts")) is dict:
        _need(set(value["source_facts"]) == set(FACTS), "source_fact_denominator_required")
    try:
        parsed = AssessmentRequest.model_validate(value).model_dump(exclude_unset=True)
        parsed["selection"] = validate_selection(parsed["selection"])
        parsed["ledger"] = parse_event_ledger(parsed["ledger"], assets=parsed["selection"]["assets"],
                                                sessions=parsed["selection"]["sessions"])
    except (ValidationError, DailyEventReviewError) as exc:
        raise RealDailyWebContractError("invalid_assessment_request" if isinstance(exc, ValidationError)
                                         else exc.code) from None
    _need(parsed["method_digest"] == method_metadata()["method_digest"], "method_drift")
    return parsed


def assessment_from_request(value):
    """Reuse admission rules, including bounded tracked-code identity reads.

    Unknown/rejected facts and event blockers remain blockers. Readiness,
    authentication, execution permission and PIT status remain separate.
    """
    p = parse_assessment_request(value)
    try:
        return build_assessment(p["selection"], p["ledger"], p["cash_annotations"],
                                p["source_facts"], p["input_pins"])
    except (RealDailyAdmissionError, RealDailyMethodError) as exc:
        raise RealDailyWebContractError(exc.code) from None


def validate_assessment_metadata(value):
    """Validate a saved assessment without rebuilding current tracked-code pins.

    This checks the frozen v1 semantics, source denominator and retained blockers;
    it does not authenticate evidence or read originals/current code files.
    """
    _finite_bound(value)
    keys = {"schema_version", "assessment_version", "selection", "ledger", "cash_annotations", "source_facts",
            "input_pins", "method", "method_digest", "event_review", "code_identity", "blockers", "status",
            "origin", "source_authenticated", "real_source_ready", "point_in_time", "local_development_execution_allowed",
            "final_numeric_access_allowed", "final_numeric_rows_parsed", "limitations", "assessment_digest"}
    _need(type(value) is dict and set(value) == keys, "invalid_assessment_schema")
    _need(type(value["schema_version"]) is int and value["schema_version"] == 1 and
          value["assessment_version"] == ADMISSION_VERSION, "invalid_assessment_version")
    _need(value["assessment_digest"] == digest({k: v for k, v in value.items() if k != "assessment_digest"}),
          "assessment_digest_drift")
    request = parse_assessment_request({"schema_version": 1, **{k: value[k] for k in
        ("selection", "ledger", "cash_annotations", "source_facts", "input_pins", "method_digest")}})
    try:
        pins = _pins(request["input_pins"], request["selection"]["assets"])
        events = assess_events(request["ledger"], selection=request["selection"], cash_annotations=request["cash_annotations"])
    except (RealDailyAdmissionError, RealDailyMethodError) as exc:
        raise RealDailyWebContractError(exc.code) from None
    evidence = {p["sha256"] for p in pins["metadata"] + pins["raw"]}
    blockers = []
    for name, fact in request["source_facts"].items():
        statement = fact["statement"]
        _need(statement is None or (statement.strip() == statement and bool(statement)), "invalid_source_fact")
        if fact["status"] == "supported":
            _need(statement is not None and fact["evidence_sha256"] in evidence, "unbound_source_evidence")
        else:
            blockers.append({"code": "source_fact_" + fact["status"], "fact": name})
    blockers.extend(deepcopy(events["blockers"]))
    code = value["code_identity"]
    _need(type(code) is dict and set(code) == {"version", "files", "digest"} and code["version"] == ADMISSION_VERSION and
          type(code["files"]) is dict and 0 < len(code["files"]) <= 50 and
          all(type(name) is str and name.startswith("paper_alpha/") and ".." not in name.split("/") for name in code["files"])
          and code["digest"] == digest(code["files"]), "invalid_code_identity")
    for sha in code["files"].values():
        try:
            MetadataPin.model_validate({"path": "tracked-code", "sha256": sha})
        except ValidationError:
            raise RealDailyWebContractError("invalid_code_identity") from None
    expected = {"schema_version": 1, "assessment_version": ADMISSION_VERSION, "selection": request["selection"],
        "ledger": request["ledger"], "cash_annotations": request["cash_annotations"], "source_facts": request["source_facts"],
        "input_pins": pins, "method": method_profile(), "method_digest": method_metadata()["method_digest"],
        "event_review": events, "code_identity": deepcopy(code), "blockers": blockers,
        "status": "blocked" if blockers else "ready_for_host_review", "origin": pins["origin"],
        "source_authenticated": False, "real_source_ready": False, "point_in_time": "not_certified",
        "local_development_execution_allowed": False, "final_numeric_access_allowed": False, "final_numeric_rows_parsed": 0,
        "limitations": ["OHLC-only raw-price method; volume units are not authenticated.",
            "Source evidence completeness is a declaration, not provider or rights authentication.",
            "Static current-vintage universe and historical PIT not certified.",
            "Cash dividends, costs, slippage and borrow are excluded from returns."]}
    expected["assessment_digest"] = digest(expected)
    _need(json_text(value) == json_text(expected), "assessment_metadata_drift")
    return deepcopy(value)


def _projection(bounds, origin):
    method = method_profile()
    return {
        "capabilities": {
            "fields": [{"name": field, "kind": "raw", "unit": "currency_major_unit", "scale": 1,
                        "adjustment": "raw_unadjusted", "available_at": AVAILABILITY[field]} for field in FIELDS],
            "operators": [{"name": name, "arity": arity} for name, arity in sorted(_FUNCTION_ARITY.items())],
            "documented_aliases": deepcopy(_ALIASES), "operator_semantics_version": OPERATOR_SEMANTICS_VERSION,
            "maximum_window": MAX_WINDOW, "data_kind": DATA_KIND, "origin": origin,
            "window_unit": "declared_calendar_sessions", "source_authenticated": False,
            "real_source_ready": False, "point_in_time": "not_certified", "local_development_execution_allowed": False,
        },
        "development": {"train": deepcopy(bounds["train"]), "validation": deepcopy(bounds["validation"]),
                        "min_assets": bounds["min_assets"], "evaluation_period": "validation",
                        "execution": deepcopy(EXECUTION), "missing_policy": method["missing_policy"],
                        "final_test_policy": FINAL_POLICY},
        "method": method_metadata(), "limitations": deepcopy(LIMITATIONS),
    }


def catalog_from_selection(selection, *, origin):
    """Independent four-field metadata projection, never a source/execute permit."""
    s = validate_selection(selection)
    _need(type(origin) is str and origin in ("engineering_fixture", "real_source_declaration"), "invalid_origin")
    bounds = {"train": s["config"]["splits"]["train"], "validation": s["config"]["splits"]["validation"],
              "min_assets": s["config"]["min_assets"]}
    value = {"schema_version": 1, "catalog_id": CATALOG_ID, "family": DATA_KIND,
             "data_contract_id": TASK_KIND, "capability_id": CAPABILITY_ID, "origin": origin,
             "method_digest": method_metadata()["method_digest"], "selection_digest": digest(s),
             "public_projection": _projection(bounds, origin)}
    value["catalog_digest"] = digest(value)
    return validate_real_daily_catalog(value, expected_selection=s)


def validate_real_daily_catalog(value, *, expected_selection=None):
    """Closed static semantics + digest; optional exact selection comparison.

    The legacy OHLCV catalog validator is neither called nor expanded.
    """
    _finite_bound(value, MAX_CATALOG_BYTES)
    _need(type(value) is dict and set(value) == {"schema_version", "catalog_id", "family", "data_contract_id",
          "capability_id", "origin", "method_digest", "selection_digest", "public_projection", "catalog_digest"},
          "invalid_catalog_schema")
    _need(type(value["schema_version"]) is int and value["schema_version"] == 1 and
          value["catalog_id"] == CATALOG_ID and value["family"] == DATA_KIND and
          value["data_contract_id"] == TASK_KIND and value["capability_id"] == CAPABILITY_ID, "catalog_identity_drift")
    _need(type(value["origin"]) is str and value["origin"] in ("engineering_fixture", "real_source_declaration"), "invalid_origin")
    for key in ("method_digest", "selection_digest", "catalog_digest"):
        try:
            MetadataPin.model_validate({"path": "host-metadata", "sha256": value[key]})
        except ValidationError:
            raise RealDailyWebContractError("invalid_catalog_digest") from None
    _need(value["catalog_digest"] == digest({k: v for k, v in value.items() if k != "catalog_digest"}), "catalog_digest_drift")
    _need(value["method_digest"] == method_metadata()["method_digest"], "method_drift")
    p = value["public_projection"]
    _need(type(p) is dict and set(p) == {"capabilities", "development", "method", "limitations"}, "invalid_catalog_projection")
    dev = p["development"]
    _need(type(dev) is dict and set(dev) == {"train", "validation", "min_assets", "evaluation_period", "execution",
          "missing_policy", "final_test_policy"}, "invalid_catalog_projection")
    try:
        train, validation = DateBounds.model_validate(dev["train"]).model_dump(), DateBounds.model_validate(dev["validation"]).model_dump()
        for bounds in (train, validation):
            _need(date.fromisoformat(bounds["start"]) <= date.fromisoformat(bounds["end"]), "invalid_catalog_development")
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, RealDailyWebContractError):
            raise
        raise RealDailyWebContractError("invalid_catalog_development") from None
    _need(train["end"] < validation["start"] and type(dev["min_assets"]) is int and 3 <= dev["min_assets"] <= 100,
          "invalid_catalog_development")
    bounds = {"train": train, "validation": validation, "min_assets": dev["min_assets"]}
    _need(json_text(p) == json_text(_projection(bounds, value["origin"])), "catalog_semantics_drift")
    if expected_selection is not None:
        s = validate_selection(expected_selection)
        _need(value["selection_digest"] == digest(s) and train == s["config"]["splits"]["train"] and
              validation == s["config"]["splits"]["validation"] and dev["min_assets"] == s["config"]["min_assets"],
              "catalog_selection_drift")
    return deepcopy(value)


def validate_execution(value, *, expected_binding=None):
    """Structural integrity only unless the host supplies its exact binding.

    This is NOT host authentication, prefix-file integrity verification or
    execution authorization. New dispatch must supply ``expected_binding`` from
    the main host registration authority and separately enforce current grants.
    """
    _finite_bound(value)
    try:
        parsed = ExecutionBinding.model_validate(value).model_dump()
    except ValidationError:
        raise RealDailyWebContractError("invalid_execution_binding") from None
    _need(parsed["source_digest"] == digest({k: v for k, v in parsed.items() if k != "source_digest"}), "execution_source_drift")
    _need(parsed["method_digest"] == method_metadata()["method_digest"], "method_drift")
    if expected_binding is not None:
        expected = validate_execution(expected_binding)
        _need(json_text(parsed) == json_text(expected), "execution_host_binding_drift")
    return parsed


def execution_binding(registration, dataset_id, assessment):
    """Produce a private flat binding from already registered host metadata.

    Does not read the prefix, rebuild current assessment/code or grant authority.
    Registration and assessment SHA values prove internal consistency, not who
    supplied them; only the host may resolve these objects from durable records.
    """
    _need(isinstance(registration, Mapping), "invalid_registration")
    r = dict(registration)
    _need(set(r) == REGISTRATION_KEYS | {"registration_digest"}, "invalid_registration_schema")
    _finite_bound(r)
    _need(type(r["schema_version"]) is int and r["schema_version"] == 1 and
          r["registration_version"] == "real-daily-ohlc-prefix-normalizer-v1" and r["status"] == "registered",
          "invalid_registration_state")
    _need(r["final_numeric_access_allowed"] is False and r["source_authenticated"] is False and
          r["real_source_ready"] is False, "registration_permission_drift")
    _need(r["registration_digest"] == digest({k: v for k, v in r.items() if k != "registration_digest"}), "registration_digest_drift")
    assessment = validate_assessment_metadata(assessment)
    _need(assessment["status"] == "ready_for_host_review" and assessment["blockers"] == [], "assessment_blocked")
    s = validate_selection(assessment["selection"])
    _need(json_text(assessment["method"]) == json_text(method_profile()) and
          assessment["method_digest"] == digest(assessment["method"]), "method_drift")
    from .real_daily_admission import _json
    try:
        saved_selection = _json(r["selection_json"]) if type(r["selection_json"]) is str else r["selection_json"]
        saved_code = _json(r["code_identity_json"]) if type(r["code_identity_json"]) is str else r["code_identity_json"]
    except RealDailyAdmissionError as exc:
        raise RealDailyWebContractError(exc.code) from None
    _need(json_text(saved_code) == json_text(assessment["code_identity"]), "registration_code_identity_drift")
    _need(json_text(validate_selection(saved_selection)) == json_text(s), "registration_selection_drift")
    _need(r["origin"] == assessment["origin"] and r["method_digest"] == assessment["method_digest"] and
          r["assessment_digest"] == assessment["assessment_digest"], "registration_assessment_drift")
    catalog = catalog_from_selection(s, origin=assessment["origin"])
    value = {"schema_version": 1, "kind": TASK_KIND, "data_kind": DATA_KIND, "scope": "development_only",
             "origin": assessment["origin"], "dataset_id": dataset_id, "registration_id": r["id"],
             "assessment_id": r["assessment_id"], "registration_digest": r["registration_digest"],
             "assessment_digest": assessment["assessment_digest"], "method_digest": assessment["method_digest"],
             "selection_digest": digest(s), "catalog_digest": catalog["catalog_digest"],
             "prefix_sha256": r["prefix_sha256"], "config_digest": digest(s["config"])}
    value["source_digest"] = digest(value)
    return validate_execution(value)
