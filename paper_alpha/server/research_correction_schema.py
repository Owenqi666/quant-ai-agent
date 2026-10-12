"""Closed correction records; local consistency is not execution authority.

These types do not perform rules, resolve a task, check real artifacts, reserve
budget or dispatch. Runtime must bind the records to the original durable loop,
parent, source, tool observation and current permissions before using them.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ..author_panel_schema import AuthorModel
from ..storage import digest
from .research_correction_policy import resolve_policy

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
Identity = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Expression = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Name = Annotated[str, StringConstraints(min_length=1, max_length=96)]
PolicyId = Literal["integral-window-representation-v1", "equivalent-expression-representation-v2"]
RuleId = Literal["exact-integral-decimal-window-v1", "direct-positive-integer-window-v1"]
NormalizationRuleId = Literal["documented-operator-alias-normalization-v1"]
ChangedPath = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"^\$\.body(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])+$")]
Repairability = Literal["equivalent", "requires_revision", "requires_information", "not_repairable", "unknown"]
DiagnosticCategory = Literal[
    "representation_equivalent", "documented_alias", "syntax_ambiguous", "missing_field",
    "unsupported_operator_or_arguments", "method_or_evidence", "data_admission_or_alignment",
    "runtime_or_integrity", "execution_unknown", "research_result", "authority_or_budget", "unknown",
]


class CorrectionPolicyReference(AuthorModel):
    """Trusted registry reference, not caller rules or a source/dispatch grant."""
    policy_id: PolicyId
    policy_digest: Sha256

    @model_validator(mode="after")
    def trusted_registry(self):
        resolve_policy(self.policy_id, self.policy_digest)
        return self


class ExpressionValidation(AuthorModel):
    used_fields: list[Name] = Field(max_length=30)
    operators: list[Name] = Field(max_length=100)
    canonical_expression: Expression


class CorrectionPlan(CorrectionPolicyReference):
    """Exact A2 14-field output; validates self-digests, never reruns the rule.

    Its field-set digest and declared tool output require an external host
    observation for authentication. No inferred fields are used for validation.
    """
    schema_version: Literal[1]
    submitted_expression: Expression
    accepted_expression: Expression
    rule_ids: list[RuleId] = Field(min_length=1, max_length=2)
    normalization_rule_ids: list[NormalizationRuleId] = Field(max_length=1)
    changed_paths: list[ChangedPath] = Field(min_length=1, max_length=128)
    expression_before_digest: Sha256
    expression_after_digest: Sha256
    available_fields_digest: Sha256
    validation: ExpressionValidation
    validation_digest: Sha256
    plan_digest: Sha256

    @model_validator(mode="after")
    def local_consistency(self):
        policy = resolve_policy(self.policy_id, self.policy_digest)
        if (len(set(self.rule_ids)) != len(self.rule_ids) or not set(self.rule_ids) <= set(policy["rule_ids"])
                or len(set(self.normalization_rule_ids)) != len(self.normalization_rule_ids)
                or len(set(self.changed_paths)) != len(self.changed_paths)):
            raise ValueError("Correction rules/paths must be unique and belong to the trusted policy")
        if (self.expression_before_digest != digest(self.submitted_expression)
                or self.expression_after_digest != digest(self.accepted_expression)
                or self.validation.canonical_expression != self.accepted_expression
                or self.validation_digest != digest(self.validation.model_dump())):
            raise ValueError("Correction expression or validation digest differs")
        value = self.model_dump()
        if self.plan_digest != digest({k: v for k, v in value.items() if k != "plan_digest"}):
            raise ValueError("Correction plan digest differs")
        return self


class CorrectionDiagnostic(CorrectionPolicyReference):
    """Host diagnostic declaration; source/tool pins are externally verified."""
    schema_version: Literal[1]
    phase: Literal["expression", "evidence", "method", "data", "source", "execution", "authority", "budget", "review"]
    code: str = Field(min_length=1, max_length=100)
    category: DiagnosticCategory
    candidate_id: Identity
    message: str = Field(min_length=1, max_length=2000)
    tool_observation_digest: Sha256
    source_digest: Sha256
    repairability: Repairability

    @model_validator(mode="after")
    def representation_label(self):
        if self.repairability == "equivalent" and (self.code != "invalid_window" or self.category != "representation_equivalent"):
            raise ValueError("Only a proved representation diagnostic may declare equivalent repairability")
        if self.category == "representation_equivalent" and self.repairability != "equivalent":
            raise ValueError("Representation category and repairability disagree")
        return self


class ProviderCorrectionAssociation(AuthorModel):
    """Association shape only; actual original Provider artifacts remain required."""
    proposal_id: str = Field(pattern=r"^proposal_[0-9a-f]{64}$")
    attempt_id: str = Field(pattern=r"^proposal_attempt_[0-9a-f]{64}$")
    response_digest: Sha256


class CorrectionReceipt(CorrectionPolicyReference):
    """Terminal structural record proposal for N2; no runtime gate is installed.

    Parent input identity and a genuine later Provider correction association
    are distinct. A deterministic receipt must not invent Provider call IDs.
    """
    schema_version: Literal[1]
    loop_id: Identity
    session_id: Identity
    candidate_id: Identity
    intent_digest: Sha256
    parent_binding_digest: Sha256
    diagnostics_digest: Sha256
    source_digest: Sha256
    submitted_expression: Expression
    accepted_expression: Expression | None
    expression_before_digest: Sha256
    expression_after_digest: Sha256 | None
    changed_paths: list[ChangedPath] = Field(max_length=128)
    rule_ids: list[RuleId] = Field(max_length=2)
    method: Literal["deterministic", "provider"]
    validation_digest: Sha256
    accepted_task_digest: Sha256 | None
    sequence: int = Field(ge=1, le=24)
    created_at: str = Field(min_length=20, max_length=40, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$")
    status: Literal["completed", "blocked"]
    provider_association: ProviderCorrectionAssociation | None = None
    receipt_digest: Sha256

    @model_validator(mode="after")
    def terminal_consistency(self):
        policy = resolve_policy(self.policy_id, self.policy_digest)
        if len(set(self.rule_ids)) != len(self.rule_ids) or not set(self.rule_ids) <= set(policy["rule_ids"]) or len(set(self.changed_paths)) != len(self.changed_paths):
            raise ValueError("Receipt rules/paths differ from the trusted policy")
        if self.expression_before_digest != digest(self.submitted_expression):
            raise ValueError("Receipt submitted-expression digest differs")
        if self.accepted_expression is None:
            if self.expression_after_digest is not None:
                raise ValueError("Absent accepted expression has no after digest")
        elif self.expression_after_digest != digest(self.accepted_expression):
            raise ValueError("Receipt accepted-expression digest differs")
        if self.status == "completed":
            if self.accepted_expression is None or self.accepted_task_digest is None or not self.rule_ids or not self.changed_paths:
                raise ValueError("Completed correction needs its expression/task and actual changes")
        elif self.accepted_expression is not None or self.accepted_task_digest is not None:
            raise ValueError("Blocked correction cannot declare an accepted expression/task")
        if (self.method == "deterministic" and self.provider_association is not None
                or self.method == "provider" and self.provider_association is None):
            raise ValueError("Provider association is required only for a real Provider correction branch")
        try:
            timestamp = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Receipt creation time must be UTC") from None
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise ValueError("Receipt creation time must be UTC")
        value = self.model_dump()
        if self.receipt_digest != digest({k: v for k, v in value.items() if k != "receipt_digest"}):
            raise ValueError("Receipt digest differs")
        return self
