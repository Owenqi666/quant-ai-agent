"""Trusted, pure policies for bounded equivalent expression representations.

Only window literals may change: exact integral decimal floats in v1, plus a
single direct positive integer literal in v2. Documented aliases retain their
existing normalization; alias-only input is never a new correction. Caller
must supply actual available fields. No draft, plan or classification grants
execution authority, consumes a slot, writes a file or dispatches a provider.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from decimal import InvalidOperation
import re

from ..expressions import (
    MAX_WINDOW, OPERATOR_SEMANTICS_VERSION, ExpressionError,
    _ALIASES, _FUNCTION_ARITY, _WINDOW_FUNCTIONS, _parse, validate_expression,
)
from ..storage import digest as storage_digest, json_text
from .provider_repair_semantics import _normalized
from .research_jobs_schema import ResearchDraft

INTEGRAL_POLICY_ID = "integral-window-representation-v1"
EQUIVALENT_POLICY_ID = "equivalent-expression-representation-v2"
POLICY_IDS = (INTEGRAL_POLICY_ID, EQUIVALENT_POLICY_ID)
DECIMAL_RULE_ID = "exact-integral-decimal-window-v1"
POSITIVE_RULE_ID = "direct-positive-integer-window-v1"
ALIAS_RULE_ID = "documented-operator-alias-normalization-v1"

# Single trusted exact-code taxonomy for both the policy fingerprint and the
# actual classifier. No client dictionary or diagnostic message is consulted.
_DIAGNOSTIC_TAXONOMY = {
    "invalid_window": {
        "requires_successful_plan": True,
        "success": {"category": "representation_equivalent", "repairability": "equivalent"},
        "fallback": {"category": "unsupported_operator_or_arguments", "repairability": "requires_revision"},
    },
    **{code: {"category": "missing_field", "repairability": "requires_information"}
       for code in ("unknown_field", "required_field_missing", "undeclared_field")},
    **{code: {"category": "unsupported_operator_or_arguments", "repairability": "requires_revision"}
       for code in ("unknown_operator", "invalid_arguments")},
    **{code: {"category": "method_or_evidence", "repairability": "requires_information"}
       for code in ("RULES_UNRESOLVED", "ATTRIBUTION_INVALID", "EVIDENCE_INVALID", "EVIDENCE_NOT_FOUND")},
    **{code: {"category": "data_admission_or_alignment", "repairability": "requires_information"}
       for code in ("SOURCE_ADMISSION_BLOCKED", "panel_alignment", "invalid_data", "NO_EVALUABLE_SAMPLES")},
    **{code: {"category": "execution_unknown", "repairability": "unknown"}
       for code in ("EXECUTION_UNKNOWN", "execution_unknown", "PROVIDER_EXECUTION_UNKNOWN")},
    **{code: {"category": "runtime_or_integrity", "repairability": "not_repairable"}
       for code in ("SOURCE_CHANGED", "SOURCE_INTEGRITY", "computation_error", "ARTIFACT_MISSING", "DIGEST_MISMATCH")},
    **{code: {"category": "authority_or_budget", "repairability": "not_repairable"}
       for code in ("BUDGET_EXHAUSTED", "APPROVAL_REVOKED", "APPROVAL_EXPIRED", "CANCELLED")},
    **{code: {"category": "research_result", "repairability": "requires_revision"}
       for code in ("WEAK_VALIDATION_RESULT", "CANDIDATE_NOT_EVALUATED", "NOT_EVALUABLE")},
    "unsafe_expression": {"category": "syntax_ambiguous", "repairability": "not_repairable"},
}
_UNKNOWN_DIAGNOSTIC = {"category": "unknown", "repairability": "unknown"}


class CorrectionPolicyError(ValueError):
    """Host configuration errors, without source expressions or private context."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise CorrectionPolicyError(code)


def get_policy(policy_id):
    """Fresh host-owned descriptor; callers cannot supply or mutate its rules."""
    _need(type(policy_id) is str and policy_id in POLICY_IDS, "CORRECTION_POLICY_UNKNOWN")
    rules = [DECIMAL_RULE_ID]
    if policy_id == EQUIVALENT_POLICY_ID:
        rules.append(POSITIVE_RULE_ID)
    value = {
        "schema_version": 1, "policy_id": policy_id, "rule_ids": rules,
        "normalization_rule_ids": [ALIAS_RULE_ID],
        "operator_semantics_version": OPERATOR_SEMANTICS_VERSION,
        "maximum_window": MAX_WINDOW,
        "window_operators": [{"name": name, "arity": _FUNCTION_ARITY[name],
                              "window_argument": _FUNCTION_ARITY[name] - 1,
                              "minimum": 0 if name == "delay" else 1, "maximum": MAX_WINDOW}
                             for name in sorted(_WINDOW_FUNCTIONS)],
        "documented_aliases": deepcopy(_ALIASES),
        "diagnostic_taxonomy_version": "exact-code-diagnostic-taxonomy-v1",
        "diagnostic_taxonomy": deepcopy(_DIAGNOSTIC_TAXONOMY),
        "unknown_diagnostic": deepcopy(_UNKNOWN_DIAGNOSTIC),
        "available_fields_policy": "actual_host_fields_required_no_expression_inference",
        "decimal_policy": "original_decimal_token_exactly_equals_same_integer",
        "positive_policy": "direct_UnaryOp_UAdd_Constant_int_only" if policy_id == EQUIVALENT_POLICY_ID else "unavailable",
        "alias_only_correction": False,
        "non_window_ast": "unchanged_except_documented_call_aliases",
        "final_validation": "complete_expression_against_actual_host_fields",
        "execution_authority": False,
    }
    value["policy_digest"] = storage_digest(value)
    return value


def policy_digest(policy_id):
    return get_policy(policy_id)["policy_digest"]


def resolve_policy(policy_id, expected_digest):
    """Exact trusted ID/digest match, never a client-defined fallback policy."""
    policy = get_policy(policy_id)
    _need(type(expected_digest) is str and re.fullmatch(r"[0-9a-f]{64}", expected_digest) is not None and
          expected_digest == policy["policy_digest"], "CORRECTION_POLICY_DIGEST_MISMATCH")
    return policy


def _actual_fields(available_fields):
    _need(type(available_fields) in (set, frozenset, list, tuple) and len(available_fields) <= 30 and
          all(type(name) is str and name.isidentifier() for name in available_fields), "AVAILABLE_FIELDS_REQUIRED")
    fields = set(available_fields)
    _need(len(fields) == len(available_fields), "AVAILABLE_FIELDS_REQUIRED")
    return fields


def _nodes(tree, path="$.body"):
    """Deterministic expression AST paths, excluding source-location attributes."""
    node = tree.body if isinstance(tree, ast.Expression) else tree
    yield path, node
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            yield from _nodes(value, path + "." + field)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, ast.AST):
                    yield from _nodes(child, path + "." + field + "[" + str(index) + "]")


def _changed_paths(original, corrected):
    original_nodes = dict(_nodes(original))
    paths, rule_ids, normalization_ids = [], [], []
    for path, node in _nodes(corrected):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        prior = original_nodes.get(path)
        if not isinstance(prior, ast.Call) or not isinstance(prior.func, ast.Name):
            continue
        if prior.func.id != node.func.id:
            paths.append(path + ".func.id")
            if ALIAS_RULE_ID not in normalization_ids:
                normalization_ids.append(ALIAS_RULE_ID)
        name = node.func.id
        if name not in _WINDOW_FUNCTIONS or len(node.args) != _FUNCTION_ARITY[name] or node.keywords:
            continue
        before, after = prior.args[-1], node.args[-1]
        if ast.dump(before, include_attributes=False) != ast.dump(after, include_attributes=False):
            paths.append(path + ".args[" + str(len(node.args) - 1) + "]")
            rule = DECIMAL_RULE_ID if isinstance(before, ast.Constant) else POSITIVE_RULE_ID
            if rule not in rule_ids:
                rule_ids.append(rule)
    return sorted(paths), sorted(rule_ids), sorted(normalization_ids)


def plan_correction(expression, available_fields, policy_id, digest):
    """Return an equivalent, fully validated host plan or ``None``.

    Unsupported expression errors return None. Unknown policy, stale policy
    digest or missing actual-field input fail with a bounded host error. This
    plan proves allowed representation changes only; it has no grant, task,
    native-repair eligibility, source authentication or budget authority.
    """
    policy = resolve_policy(policy_id, digest)
    fields = _actual_fields(available_fields)
    if type(expression) is not str:
        return None
    try:
        original = _parse(expression)
        corrected, decimal_count = _normalized(expression, convert_windows=True)
        positive_count = 0
        if policy_id == EQUIVALENT_POLICY_ID:
            for node in ast.walk(corrected):
                if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name)
                        or node.func.id not in _WINDOW_FUNCTIONS or node.keywords
                        or len(node.args) != _FUNCTION_ARITY[node.func.id]):
                    continue
                window = node.args[-1]
                if (isinstance(window, ast.UnaryOp) and isinstance(window.op, ast.UAdd)
                        and isinstance(window.operand, ast.Constant) and type(window.operand.value) is int):
                    minimum = 0 if node.func.id == "delay" else 1
                    if not minimum <= window.operand.value <= MAX_WINDOW:
                        return None
                    node.args[-1] = ast.copy_location(ast.Constant(window.operand.value), window)
                    positive_count += 1
        if decimal_count + positive_count == 0:
            return None
        accepted = ast.unparse(corrected.body)
        validation = validate_expression(accepted, fields)
        # Canonical unparse must preserve the complete corrected AST, including
        # every non-window constant type, field, sign and operand order.
        if ast.dump(_parse(validation["canonical_expression"]), include_attributes=False) != ast.dump(corrected, include_attributes=False):
            return None
        paths, rules, normalization = _changed_paths(original, corrected)
        if not paths or not rules or not set(rules) <= set(policy["rule_ids"]):
            return None
        plan = {"schema_version": 1, "policy_id": policy_id, "policy_digest": policy["policy_digest"],
                "submitted_expression": expression, "accepted_expression": validation["canonical_expression"],
                "rule_ids": rules, "normalization_rule_ids": normalization, "changed_paths": paths,
                "expression_before_digest": storage_digest(expression),
                "expression_after_digest": storage_digest(validation["canonical_expression"]),
                "available_fields_digest": storage_digest(sorted(fields)),
                "validation": validation, "validation_digest": storage_digest(validation)}
        plan["plan_digest"] = storage_digest(plan)
        return plan
    except (ValueError, TypeError, SyntaxError, InvalidOperation, OverflowError, RecursionError):
        return None


def _draft(value):
    parsed = ResearchDraft.model_validate(value).model_dump()
    # Reject coercions or omitted semantic properties rather than repair them.
    if json_text(parsed) != json_text(value):
        raise ValueError("Draft differs from strict parsed representation")
    return parsed


def correct_draft(parent_draft, *, candidate_id, available_fields, policy_id, digest):
    """Pure single-candidate helper; all non-expression draft bytes stay equal.

    The entire resulting candidate set must be legal against the supplied real
    field set. Evidence/hypothesis truth and lineage still need domain validation.
    """
    resolve_policy(policy_id, digest)
    fields = _actual_fields(available_fields)
    try:
        parent = _draft(parent_draft)
        selected = [item for item in parent["candidates"] if item["id"] == candidate_id]
        if len(selected) != 1 or len({item["id"] for item in parent["candidates"]}) != len(parent["candidates"]):
            return None
        plan = plan_correction(selected[0]["expression"], fields, policy_id, digest)
        if plan is None:
            return None
        repaired = deepcopy(parent)
        next(item for item in repaired["candidates"] if item["id"] == candidate_id)["expression"] = plan["accepted_expression"]
        hypotheses = {item["id"]: item for item in parent["hypotheses"]}
        if len(hypotheses) != len(parent["hypotheses"]):
            return None
        for item in repaired["candidates"]:
            hypothesis = hypotheses[item["hypothesis_id"]]
            required = set(hypothesis["required_fields"])
            if not required <= fields:
                return None
            validation = validate_expression(item["expression"], fields)
            if not set(validation["used_fields"]) <= required:
                return None
        frozen = deepcopy(repaired)
        next(item for item in frozen["candidates"] if item["id"] == candidate_id)["expression"] = selected[0]["expression"]
        if json_text(frozen) != json_text(parent):
            return None
        return {"draft": repaired, "plan": plan}
    except (ValueError, TypeError, KeyError, SyntaxError, InvalidOperation, OverflowError, RecursionError):
        return None


def validate_correction_output(parent_draft, new_draft, *, candidate_id, available_fields, policy_id, digest):
    """Check proposed output against the unique permitted plan, no effects."""
    resolve_policy(policy_id, digest)
    fields = _actual_fields(available_fields)
    rejected = [{"code": "CORRECTION_SEMANTICS_CHANGED", "message": "Output must match the host's exact equivalent expression plan and preserve all research assertions."}]
    try:
        expected = correct_draft(parent_draft, candidate_id=candidate_id, available_fields=fields, policy_id=policy_id, digest=digest)
        if expected is None:
            return rejected
        new = _draft(new_draft)
        original = _draft(parent_draft)
        selected = [item for item in new["candidates"] if item["id"] == candidate_id]
        if len(selected) != 1:
            return rejected
        tree, _ = _normalized(selected[0]["expression"], convert_windows=False)
        actual_validation = validate_expression(ast.unparse(tree.body), fields)
        if ast.dump(tree, include_attributes=False) != ast.dump(_parse(expected["plan"]["accepted_expression"]), include_attributes=False):
            return rejected
        selected[0]["expression"] = actual_validation["canonical_expression"]
        if json_text(new) != json_text(expected["draft"]):
            return rejected
        frozen = deepcopy(new)
        next(item for item in frozen["candidates"] if item["id"] == candidate_id)["expression"] = next(
            item["expression"] for item in original["candidates"] if item["id"] == candidate_id)
        return [] if json_text(frozen) == json_text(original) else rejected
    except (ValueError, TypeError, KeyError, SyntaxError, InvalidOperation, OverflowError, RecursionError):
        return rejected


def classify_diagnostic(code, expression, available_fields, policy_id, digest):
    """Host-only exact-code classification; messages never determine repairability.

    An invalid-window classification invokes the pure correction rule. Runtime
    must call this only after its durable correction intent/slot, never from a
    read-only observer/get path. This helper cannot itself enforce that ledger.
    """
    policy = resolve_policy(policy_id, digest)
    fields = _actual_fields(available_fields)
    _need(type(code) is str and 0 < len(code) <= 100, "DIAGNOSTIC_CODE_REQUIRED")
    plan = None
    # Use the registry-owned snapshot whose taxonomy was included in the exact
    # validated policy digest. Never accept a caller-supplied classification map.
    disposition = policy["diagnostic_taxonomy"].get(code, policy["unknown_diagnostic"])
    if disposition.get("requires_successful_plan") is True:
        plan = plan_correction(expression, fields, policy_id, digest)
        disposition = disposition["success" if plan is not None else "fallback"]
    category, repairability = disposition["category"], disposition["repairability"]
    return {"schema_version": 1, "code": code, "category": category, "repairability": repairability,
            "policy_id": policy_id, "policy_digest": policy["policy_digest"], "plan": plan}
