"""Offline research-quality-v1 contracts and mechanical checks.

This first version admits engineering fixtures only. It does not dispatch models,
execute factors, authenticate receipts/reviewers, or assign scientific quality.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import re

from .expressions import ExpressionError, validate_expression
from .storage import digest, json_text, read_json

PROTOCOL_ID = "research-quality-v1"
MAX_BYTES = 2 * 1024 * 1024
MAX_CASES = 40
MODES = ("fixed", "deterministic_repair", "model_assisted")
IDENTITIES = ("source", "task", "data", "config", "engine", "capability", "exposure")
STATUSES = {"completed", "failed", "abstained", "cancelled", "budget_stopped", "not_run"}
KINDS = {"citation", "expression", "attribution", "report_metric"}
USAGE_FIELDS = ("provider_attempts", "backend_model_calls", "input_tokens", "output_tokens", "currency_cost")
DIMENSIONS = {"evidence_accuracy", "hypothesis_fidelity", "implementation_alignment", "field_semantics", "mechanism_attribution"}


class ResearchQualityError(ValueError):
    """An invalid/foreign declaration is rejected before it can receive a score."""


def _require(condition, message):
    if not condition:
        raise ResearchQualityError(message)


def _fields(value, keys, name):
    _require(type(value) is dict and set(value) == set(keys), f"Unknown/missing {name} fields")


def _identifier(value, name="identifier"):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value), f"Invalid {name}")


def _text(value, name):
    _require(type(value) is str and value.strip() and len(value) <= 16384, f"Invalid {name}")


def _sha(value, name):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), f"Invalid {name} SHA256")


def _number(value, name, integer=False):
    allowed = (int,) if integer else (int, float)
    _require(type(value) in allowed, f"Nonfinite/invalid {name}")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    _require(finite, f"Nonfinite/invalid {name}")


def _names(values, name, empty=True):
    _require(type(values) is list and (empty or bool(values)) and len(values) <= 256, f"Invalid {name} inventory")
    for value in values:
        _identifier(value, name)
    _require(len(values) == len(set(values)), f"Duplicate {name}")


def _bounded(value):
    try:
        size = len(json_text(value).encode("utf8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResearchQualityError("Noncanonical/nonfinite quality input") from exc
    _require(size <= MAX_BYTES, "Quality input exceeds byte budget")


def _index(values, name, maximum=5000):
    _require(type(values) is list and len(values) <= maximum, f"Invalid {name} list")
    result = {}
    for row in values:
        _require(type(row) is dict and type(row.get("id")) is str, f"Invalid {name} identity")
        _require(row["id"] not in result, f"Duplicate {name} id")
        result[row["id"]] = row
    return result


def _provider_view(protocol, case):
    source = next(row for row in protocol["sources"] if row["id"] == case["source_id"])
    # Construct a whitelist, never filter a host-private case/protocol dictionary.
    return {"case_id": case["id"], "task_prompt": case["task"]["prompt"],
            "source": {"id": source["id"], "sha256": source["sha256"],
                       "media_type": source["media_type"], "physical_page": source["physical_page"], "text": source["text"]},
            "capabilities": {"fields": list(protocol["capabilities"]["fields"]),
                             "operators": list(protocol["capabilities"]["operators"])}}


def validate_protocol(protocol: dict) -> dict:
    """Validate a frozen host-private catalogue without executing any task."""
    _bounded(protocol)
    _fields(protocol, {"schema_version", "protocol_id", "version", "scope", "repetitions", "modes", "sources", "cases", "pending_materials", "capabilities", "baseline_policy", "reference_policy"}, "protocol")
    _require(type(protocol["schema_version"]) is int and protocol["schema_version"] == 1, "Unsupported quality schema")
    _require(protocol["protocol_id"] == PROTOCOL_ID and protocol["version"] == "1", "Unsupported quality protocol")
    _require(protocol["scope"] == "engineering_fixture_only", "This version has no qualified independent research adapter")
    _require(type(protocol["repetitions"]) is int and 1 <= protocol["repetitions"] <= 3, "Invalid repetition budget")
    _require(protocol["modes"] == list(MODES), "Frozen baseline modes differ")
    baseline = protocol["baseline_policy"]
    _fields(baseline, {"id", "comparison_dimensions", "expected_labels_visibility", "model_execution"}, "baseline policy")
    _require(baseline == {"id": "same-input-quality-v1", "comparison_dimensions": list(IDENTITIES), "expected_labels_visibility": "host_private", "model_execution": "not_run"}, "Baseline/exposure policy differs")
    _fields(protocol["reference_policy"], {"semantic_score", "mechanical_scope", "human_labels_created"}, "reference policy")
    _require(protocol["reference_policy"] == {"semantic_score": "pending_independent_reference", "mechanical_scope": "engineering_fixture_only", "human_labels_created": False} and protocol["reference_policy"]["human_labels_created"] is False, "Reference policy differs")
    caps = protocol["capabilities"]
    _fields(caps, {"fields", "operators", "sha256"}, "capability")
    _names(caps["fields"], "available field", empty=False)
    _names(caps["operators"], "available operator")
    _sha(caps["sha256"], "capability")
    _require(caps["sha256"] == digest({key: caps[key] for key in ("fields", "operators")}), "Capability content identity differs")
    sources = _index(protocol["sources"], "source", MAX_CASES)
    _require(bool(sources), "No declared engineering source")
    for sid, source in sources.items():
        _identifier(sid, "source id")
        _fields(source, {"id", "origin", "media_type", "sha256", "physical_page", "text"}, "source")
        _require(source["origin"] == "engineering_fixture" and source["media_type"] == "text/plain", "No certified real-paper source in this adapter")
        _text(source["text"], "source text")
        _sha(source["sha256"], "source")
        _require(hashlib.sha256(source["text"].encode("utf8")).hexdigest() == source["sha256"], "Source content SHA differs")
        _require(type(source["physical_page"]) is int and source["physical_page"] == 1, "Fixture page must be explicitly synthetic page 1")
    cases = _index(protocol["cases"], "case", MAX_CASES)
    _require(bool(cases), "No ready engineering cases")
    for cid, case in cases.items():
        _identifier(cid, "case id")
        _fields(case, {"id", "partition", "state", "source_id", "task", "identities", "checks", "references", "expected_run_status_by_mode"}, "case")
        _require(case["partition"] == "development_control" and case["state"] == "ready", "Only ready development fixtures are executable")
        _require(case["source_id"] in sources, "Foreign case source")
        _fields(case["task"], {"prompt", "sha256"}, "task")
        _text(case["task"]["prompt"], "task prompt")
        _require(case["task"]["sha256"] == digest({"prompt": case["task"]["prompt"]}), "Task content identity differs")
        _fields(case["identities"], IDENTITIES, "case identity")
        for key, value in case["identities"].items():
            _sha(value, key)
        _require(case["identities"]["source"] == sources[case["source_id"]]["sha256"] and case["identities"]["task"] == case["task"]["sha256"] and case["identities"]["capability"] == caps["sha256"], "Source/task/capability binding differs")
        _require(case["identities"]["engine"] == hashlib.sha256(Path(__file__).with_name("expressions.py").read_bytes()).hexdigest(), "Actual expression-check engine identity differs")
        _require(case["identities"]["exposure"] == digest(_provider_view(protocol, case)), "Frozen generator exposure differs")
        _fields(case["expected_run_status_by_mode"], MODES, "expected run status")
        _require(all(type(status) is str and status in STATUSES for status in case["expected_run_status_by_mode"].values()), "Invalid frozen terminal status")
        _require(case["expected_run_status_by_mode"]["model_assisted"] == "not_run", "Model-assisted execution is pending in fixture adapter")
        checks = _index(case["checks"], "check", 100)
        _require(bool(checks), "Every ready case needs frozen check IDs")
        for check_id, check in checks.items():
            _identifier(check_id, "check id")
            _fields(check, {"id", "kind", "expected", "target"}, "check")
            _require(type(check["kind"]) is str and check["kind"] in KINDS and type(check["expected"]) is str and check["expected"] in {"passed", "failed", "rejected"}, "Invalid check kind/engineering expectation")
            target = check["target"]
            if check["kind"] == "citation":
                _fields(target, {"source_id", "physical_page", "start_char", "end_char", "quote"}, "citation target")
                _require(target["source_id"] == case["source_id"], "Foreign citation source")
                source = sources[target["source_id"]]
                _require(type(target["physical_page"]) is int and target["physical_page"] == source["physical_page"], "Frozen page differs")
                _require(type(target["start_char"]) is int and type(target["end_char"]) is int and 0 <= target["start_char"] < target["end_char"] <= len(source["text"]), "Invalid frozen source span")
                _require(source["text"][target["start_char"]:target["end_char"]] == target["quote"], "Frozen quote differs from source span")
            elif check["kind"] == "expression":
                _fields(target, {"required_fields"}, "expression target")
                _names(target["required_fields"], "required field")
            elif check["kind"] == "attribution":
                _fields(target, {"source_id", "allowed_origins"}, "attribution target")
                _require(target["source_id"] == case["source_id"], "Foreign attribution source")
                _names(target["allowed_origins"], "allowed origin", empty=False)
                _require(set(target["allowed_origins"]) <= {"paper_original", "user_modification", "model_conjecture", "project_convention"}, "Unknown attribution origin")
            else:
                _fields(target, {"result_id", "metric_key", "result_sha256"}, "metric target")
                _identifier(target["result_id"], "result id")
                _identifier(target["metric_key"], "metric key")
                _sha(target["result_sha256"], "result")
        references = _index(case["references"], "reference", 100)
        active = {}
        for rid, ref in references.items():
            _identifier(rid, "reference id")
            _fields(ref, {"id", "dimension", "source", "independence", "status", "outcome", "author", "version", "exposed_to_generator", "source_sha256"}, "reference")
            _require(type(ref["dimension"]) is str and ref["dimension"] in DIMENSIONS and type(ref["source"]) is str and ref["source"] in {"unknown", "automation", "paper", "independent_numeric"}, "Fixture references cannot invent human labels")
            _require(type(ref["independence"]) is str and ref["independence"] in {"unknown", "not_established", "declared_independent"} and type(ref["status"]) is str and ref["status"] in {"unknown", "conflict", "active"}, "Invalid reference provenance/state")
            _require(type(ref["exposed_to_generator"]) is bool, "Reference exposure must be explicit")
            if ref["source_sha256"] is not None:
                _sha(ref["source_sha256"], "reference source")
            if ref["source"] != "unknown":
                _text(ref["author"], "reference author")
                _text(ref["version"], "reference version")
            else:
                _require(ref["author"] is None and ref["version"] is None, "Unknown reference author/version must stay null")
            if ref["status"] == "active":
                _require(ref["source"] != "unknown" and type(ref["outcome"]) is str and ref["outcome"] in {"supported", "contradicted"}, "Active reference has no adjudicated declaration")
                prior = active.setdefault(ref["dimension"], ref["outcome"])
                _require(prior == ref["outcome"], "Conflicting active references require adjudication")
            else:
                _require(ref["outcome"] is None, "Unknown/conflict reference cannot supply a score")
    pending = _index(protocol["pending_materials"], "pending material", MAX_CASES)
    _require(not set(pending) & set(cases), "Pending and ready identities overlap")
    for row in pending.values():
        _fields(row, {"id", "state", "source_identity", "reference_identity", "reason"}, "pending material")
        _identifier(row["id"], "pending id")
        _require(row["state"] == "awaiting_independent_material" and row["source_identity"] is None and row["reference_identity"] is None, "Pending slot must not invent a paper/reference identity")
        _text(row["reason"], "pending reason")
    return deepcopy(protocol)


def load_protocol(path: Path) -> dict:
    path = Path(path)
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BYTES, "Missing/symlinked/unbounded protocol")
    return validate_protocol(read_json(path))


def provider_view(protocol: dict, case_id: str) -> dict:
    """Original material/task/capabilities only; no labels, results or private paths."""
    protocol = validate_protocol(protocol)
    case = next((row for row in protocol["cases"] if row["id"] == case_id), None)
    _require(case is not None, "Foreign provider-view case")
    return _provider_view(protocol, case)


def _submission(kind, value):
    if kind == "citation":
        _fields(value, {"source_id", "source_sha256", "physical_page", "start_char", "end_char", "quote"}, "submitted citation")
        _identifier(value["source_id"], "submitted source")
        _sha(value["source_sha256"], "submitted source")
        _require(all(type(value[key]) is int for key in ("physical_page", "start_char", "end_char")), "Citation offsets/page must be integers")
        _text(value["quote"], "submitted quote")
    elif kind == "expression":
        _fields(value, {"expression", "required_fields"}, "submitted expression")
        _text(value["expression"], "expression")
        _names(value["required_fields"], "submitted required field")
    elif kind == "attribution":
        _fields(value, {"source_id", "origin"}, "submitted attribution")
        _identifier(value["source_id"], "submitted source")
        _identifier(value["origin"], "submitted origin")
    else:
        _fields(value, {"result_id", "metric_key", "value"}, "submitted metric")
        _identifier(value["result_id"], "submitted result")
        _identifier(value["metric_key"], "submitted metric")
        _number(value["value"], "submitted metric")


def _check(protocol, run_id, check, submitted, results):
    kind, target = check["kind"], check["target"]
    if submitted is None:
        return {"outcome": "not_run", "reason_code": "missing_check", "tool_value": None}
    _submission(kind, submitted)
    if kind == "citation":
        source = next(row for row in protocol["sources"] if row["id"] == target["source_id"])
        _require(submitted["source_id"] == target["source_id"], "Foreign submitted citation source")
        expected = dict(target, source_sha256=source["sha256"])
        passed = submitted == expected
        return {"outcome": "passed" if passed else "failed", "reason_code": "source_page_span_quote_match" if passed else "citation_binding_mismatch", "tool_value": None}
    if kind == "attribution":
        _require(submitted["source_id"] == target["source_id"], "Foreign submitted attribution source")
        _require(submitted["origin"] in {"paper_original", "user_modification", "model_conjecture", "project_convention"}, "Unknown submitted attribution origin")
        passed = submitted["origin"] in target["allowed_origins"]
        return {"outcome": "passed" if passed else "failed", "reason_code": "declared_attribution_match" if passed else "attribution_mismatch", "tool_value": None}
    if kind == "expression":
        try:
            checked = validate_expression(submitted["expression"], set(protocol["capabilities"]["fields"]))
        except ExpressionError as exc:
            return {"outcome": "rejected", "reason_code": exc.code, "tool_value": None}
        passed = (set(submitted["required_fields"]) == set(target["required_fields"]) == set(checked["used_fields"]) and set(checked["operators"]) <= set(protocol["capabilities"]["operators"]))
        return {"outcome": "passed" if passed else "failed", "reason_code": "expression_fields_operators_valid" if passed else "field_or_operator_declaration_mismatch", "tool_value": None}
    _require(submitted["result_id"] == target["result_id"] and submitted["metric_key"] == target["metric_key"], "Foreign report metric reference")
    result = results.get(f"{run_id}/{target['result_id']}")
    if result is None:
        return {"outcome": "unknown", "reason_code": "missing_host_tool_result", "tool_value": None}
    value = result["values"].get(target["metric_key"])
    if value is None:
        return {"outcome": "unknown", "reason_code": "missing_host_metric", "tool_value": None}
    passed = submitted["value"] == value
    return {"outcome": "passed" if passed else "failed", "reason_code": "host_tool_metric_match" if passed else "reported_metric_mismatch", "tool_value": value}


def _ratio(passed, expected):
    return {"numerator": len(passed), "denominator": len(expected), "rate": len(passed) / len(expected) if expected else None, "passed_ids": sorted(passed), "eligible_ids": sorted(expected)}


def evaluate(protocol: dict, observations: dict) -> dict:
    """Check host-bound observations against all frozen slots, including missing ones."""
    protocol = validate_protocol(protocol)
    _bounded(observations)
    _fields(observations, {"schema_version", "protocol_id", "protocol_sha256", "runs", "tool_results"}, "observation envelope")
    _require(type(observations["schema_version"]) is int and observations["schema_version"] == 1 and observations["protocol_id"] == PROTOCOL_ID and observations["protocol_sha256"] == digest(protocol), "Observation protocol identity differs")
    plan = {f"{case['id']}/{mode}/{repeat}": (case, mode, repeat) for case in protocol["cases"] for mode in MODES for repeat in range(1, protocol["repetitions"] + 1)}
    runs = _index(observations["runs"], "run")
    _require(set(runs) <= set(plan), "Foreign/extra run ID")
    expected_results = {}
    for run_id, (case, _, _) in plan.items():
        for check in case["checks"]:
            if check["kind"] == "report_metric":
                target = check["target"]
                identity = f"{run_id}/{target['result_id']}"
                declaration = expected_results.setdefault(identity, {"sha256": target["result_sha256"], "keys": set()})
                _require(declaration["sha256"] == target["result_sha256"], "Conflicting frozen host result identities")
                declaration["keys"].add(target["metric_key"])
    results = _index(observations["tool_results"], "host result")
    _require(set(results) <= set(expected_results), "Foreign/extra host result ID")
    for identity, result in results.items():
        _fields(result, {"id", "run_id", "origin", "values", "sha256"}, "host result")
        _require(result["run_id"] in runs and identity.startswith(result["run_id"] + "/") and runs[result["run_id"]].get("status") != "not_run", "Host result belongs to missing/foreign/unrun case")
        _require(result["origin"] == "engineering_fixture", "Unqualified host result provenance")
        _require(type(result["values"]) is dict and set(result["values"]) <= expected_results[identity]["keys"], "Unknown host metric keys")
        for key, value in result["values"].items():
            _number(value, f"host metric {key}")
        _require(result["sha256"] == digest({"origin": result["origin"], "values": result["values"]}) == expected_results[identity]["sha256"], "Frozen host result digest differs")
    rows, check_ids, mechanical_passed, handling_passed, recorded, status_passed = [], [], [], [], [], []
    usage_samples = {key: [] for key in USAGE_FIELDS}
    wall_samples = []
    for run_id, (case, mode, repeat) in plan.items():
        run = runs.get(run_id)
        checks = {}
        status = "not_run"
        if run is not None:
            _fields(run, {"id", "case_id", "mode", "repeat", "status", "identities", "checks", "usage", "tool_wall_seconds"}, "run")
            _require(run["case_id"] == case["id"] and run["mode"] == mode and type(run["repeat"]) is int and run["repeat"] == repeat, "Foreign run binding")
            _fields(run["identities"], IDENTITIES, "run identity")
            _require(run["identities"] == case["identities"], "Baseline data/config/engine/capability/exposure identity differs")
            _require(type(run["status"]) is str and run["status"] in STATUSES, "Unknown terminal status")
            status = run["status"]
            _require(mode != "model_assisted" or status == "not_run", "Model-assisted execution is not qualified in fixture adapter")
            checks = _index(run["checks"], "submitted check", 100)
            _require(set(checks) <= {c["id"] for c in case["checks"]}, "Foreign/extra submitted check ID")
            _require(status != "not_run" or not checks, "Unrun case cannot submit checks")
            for submitted in checks.values():
                _fields(submitted, {"id", "submission"}, "submitted check")
            _fields(run["usage"], USAGE_FIELDS, "usage")
            for key, value in run["usage"].items():
                if value is not None:
                    _number(value, key, integer=key != "currency_cost")
                    _require(value >= 0, "Negative usage")
                    if key in {"provider_attempts", "backend_model_calls"}:
                        _require(value == 0, "Project model dispatches are forbidden in fixture observations")
                    usage_samples[key].append(value)
            if run["tool_wall_seconds"] is not None:
                _number(run["tool_wall_seconds"], "tool wall seconds")
                _require(run["tool_wall_seconds"] >= 0, "Negative tool wall time")
                wall_samples.append(run["tool_wall_seconds"])
            if status != "not_run":
                recorded.append(run_id)
                if status == case["expected_run_status_by_mode"][mode]:
                    status_passed.append(run_id)
        result_checks = []
        for check in case["checks"]:
            identity = f"{run_id}/{check['id']}"
            check_ids.append(identity)
            observed = _check(protocol, run_id, check, checks.get(check["id"], {}).get("submission"), results)
            if observed["outcome"] == "passed":
                mechanical_passed.append(identity)
            matched = observed["outcome"] == check["expected"]
            if matched:
                handling_passed.append(identity)
            result_checks.append({"id": identity, "kind": check["kind"], "expected": check["expected"], "engineering_expectation_matched": matched, **observed})
        rows.append({"id": run_id, "case_id": case["id"], "mode": mode, "repeat": repeat, "status": status, "identities": deepcopy(case["identities"]), "checks": result_checks})
    references = [{"case_id": case["id"], **deepcopy(ref)} for case in protocol["cases"] for ref in case["references"]]
    return {"schema_version": 1, "protocol_id": PROTOCOL_ID, "protocol_sha256": digest(protocol), "scope": "engineering_fixture_only",
            "planned_runs": len(plan), "declared_cases": len(protocol["cases"]), "declared_checks": len(check_ids),
            "recorded_runs": _ratio(recorded, list(plan)), "terminal_status_handling": _ratio(status_passed, list(plan)),
            "mechanical_checks": _ratio(mechanical_passed, check_ids), "engineering_expectation_handling": _ratio(handling_passed, check_ids),
            "status_counts": dict(Counter(row["status"] for row in rows)), "rows": rows,
            "semantic": {"quality_score": None, "status": "pending_independent_reference", "human_labels_created": False, "independent_papers_evaluated": 0, "assessment_execution": "not_run", "reference_layers": references},
            "pending_materials": deepcopy(protocol["pending_materials"]),
            "usage": {key: {"known_subtotal": sum(values) if values else None, "known_samples": len(values), "planned_runs": len(plan), "total": sum(values) if len(values) == len(plan) else None} for key, values in usage_samples.items()},
            "tool_wall_seconds": {"known_subtotal": sum(wall_samples) if wall_samples else None, "known_samples": len(wall_samples), "planned_runs": len(plan), "total": sum(wall_samples) if len(wall_samples) == len(plan) else None},
            "human_efficiency": None, "model_quality_evaluated": False,
            "limitations": ["Source/receipt hashes prove local declared byte consistency, not authenticated source, tool execution or reviewer independence.", "Citation checks compare synthetic page/span/quote bindings; expression checks validate syntax/declared fields/operators, not hypothesis fidelity.", "Report numbers resolve only frozen host fixture results; no factor execution or independent numerical reproduction occurs.", "Expected labels are host-private engineering handling declarations; negative rejection is separate from raw mechanical passing.", "All model-assisted slots are fixture/unrun declarations, never project model dispatches or blind research observations.", "Independent papers, independent scientific references and human evaluation remain pending; unknown usage and human time stay null."]}
