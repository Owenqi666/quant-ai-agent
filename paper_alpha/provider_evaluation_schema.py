"""Closed contracts for aggregation fixtures, not research execution evidence."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')]
Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Outcome = Literal['completed', 'abstained', 'rejected', 'failed', 'interrupted', 'exhausted', 'blocked', 'not_run']


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class FileIdentity(EvaluationModel):
    sha256: Hash
    bytes: int = Field(ge=1, le=64 * 1024 * 1024)


class InputReference(EvaluationModel):
    path: str = Field(min_length=1, max_length=300)
    identity: FileIdentity
    scope: Literal['historical_baseline', 'maintained_formula_input', 'frozen_provider_contract']


class FixtureReference(EvaluationModel):
    path: str = Field(min_length=1, max_length=200)
    identity: FileIdentity


class EvaluationCase(EvaluationModel):
    id: Identifier
    group: Literal['generation', 'abstention', 'transport', 'validation', 'budget', 'recovery', 'material', 'repair', 'decision', 'reserved']
    partition: Literal['development', 'reserved']
    availability: Literal['fixture_ready', 'blocked']
    requires: list[Identifier] = Field(min_length=1, max_length=10)
    blocked_reason: Text | None
    role: Literal['positive', 'negative', 'planning']
    allowed_outcomes: list[Outcome] = Field(min_length=1, max_length=8)
    citation_required: bool
    numerical_required: bool
    formula_family: Identifier | None
    paper_lineage: Identifier | None
    baseline_policy: Literal['implementation_equivalence', 'variant_comparison', 'not_applicable', 'strategy_pending']
    fixture: FixtureReference | None
    label_source: Literal['automation_engineering']
    human_reference: Literal['pending']
    note: Text

    @model_validator(mode='after')
    def consistent(self):
        if len(set(self.requires)) != len(self.requires) or len(set(self.allowed_outcomes)) != len(self.allowed_outcomes):
            raise ValueError('Case requirements and outcomes must be unique')
        if self.availability == 'fixture_ready':
            if self.fixture is None or self.blocked_reason is not None or self.partition != 'development':
                raise ValueError('Ready cases require a fixture and no blocked reason')
        elif self.fixture is not None or self.blocked_reason is None:
            raise ValueError('Blocked cases cannot invent runnable fixtures')
        if self.role == 'planning' and self.availability != 'blocked':
            raise ValueError('Planning cases are blocked until their business contract exists')
        return self


class EvaluationManifest(EvaluationModel):
    schema_version: Literal[1]
    suite_id: Literal['provider_dev_v1']
    inputs: dict[Identifier, InputReference] = Field(min_length=1, max_length=20)
    cases: list[EvaluationCase] = Field(min_length=1, max_length=30)


class ProtocolBudget(EvaluationModel):
    planned_real_provider_dispatches: Literal[0]
    planned_actual_business_runs: Literal[0]
    max_fixture_records: int = Field(ge=1, le=90)
    max_record_bytes: int = Field(ge=1024, le=65536)
    max_total_artifact_bytes: int = Field(ge=65536, le=8 * 1024 * 1024)
    max_currency_cost: None
    hard_backend_model_call_cap_supported: Literal[False]
    real_execution_plan: None


class EvaluationProtocol(EvaluationModel):
    schema_version: Literal[1]
    protocol_id: Literal['provider-development-aggregation-v1']
    suite_id: Literal['provider_dev_v1']
    measurement_scope: Literal['aggregation_fixture']
    manifest: Literal['manifest.json']
    catalog: Literal['case_catalog.json']
    repetitions: int = Field(ge=1, le=3)
    modes: list[Literal['fixture']] = Field(min_length=1, max_length=1)
    future_business_modes: list[Literal['normalized_fixed', 'provider_one_shot', 'provider_bounded_repair']] = Field(min_length=3, max_length=3)
    real_execution_supported: Literal[False]
    queue_exercised: Literal[False]
    budget: ProtocolBudget
    label_policy: Literal['engineering_declarations_not_human_semantic_labels']
    final_test_execution: Literal['locked']
    human_evaluation: Literal['deferred_until_ai_and_ui_stable']
    baseline_rule: Text
    missing_usage_rule: Text
    input_boundary: Text
    limitations: list[Text] = Field(min_length=1, max_length=20)


class FixtureObservation(EvaluationModel):
    schema_version: Literal[1]
    case_id: Identifier
    measurement_scope: Literal['aggregation_fixture']
    outcome: Outcome
    reason_code: Identifier
    simulated_provider_invocations: int = Field(ge=0, le=3)
    simulated_experiments: int = Field(ge=0, le=3)
    simulated_response: Literal['accepted', 'rejected', 'missing', 'not_requested']
    simulated_schema_valid: bool | None
    simulated_citation_valid: bool | None
    simulated_numerical: Literal['passed', 'failed', 'not_comparable', 'not_run']
    simulated_baseline: Literal['equivalent', 'variant_comparison', 'not_comparable', 'not_run']
    simulated_projection_digest: Hash | None
    actual_provider_dispatches: Literal[0]
    actual_business_runs: Literal[0]
    backend_model_calls: Literal[0]
    usage: None
    cost: None
    human_reference: Literal['pending']
    semantic_fidelity: Literal['unverified']

    @model_validator(mode='after')
    def consistent(self):
        if self.simulated_response == 'not_requested' and self.simulated_provider_invocations:
            raise ValueError('Unrequested fixture cannot declare a simulated invocation')
        if self.simulated_response == 'missing' and self.simulated_schema_valid is not None:
            raise ValueError('Missing response cannot declare schema success or failure')
        if self.simulated_numerical in {'passed', 'failed'} and not self.simulated_experiments:
            raise ValueError('Numerical fixture requires a simulated experiment')
        if self.outcome == 'not_run' and (self.simulated_provider_invocations or self.simulated_experiments):
            raise ValueError('Not-run fixtures cannot have simulated effects')
        if self.outcome == 'not_run' and (self.simulated_schema_valid is not None or self.simulated_citation_valid is not None
                or self.simulated_numerical != 'not_run' or self.simulated_baseline != 'not_run'
                or self.simulated_projection_digest is not None):
            raise ValueError('Not-run fixtures cannot invent check results')
        return self


class FixtureRecord(FixtureObservation):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}/fixture/[1-3]$')
    repetition: int = Field(ge=1, le=3)


class EvaluationPlan(EvaluationModel):
    schema_version: Literal[1]
    kind: Literal['provider_evaluation_plan']
    id: str = Field(pattern=r'^provider_plan_[a-f0-9]{64}$')
    digest: Hash
    protocol_digest: Hash
    measurement_scope: Literal['aggregation_fixture']
    queue_exercised: Literal[False]
    real_execution_supported: Literal[False]
    planned_real_provider_dispatches: Literal[0]
    planned_actual_business_runs: Literal[0]
    snapshots: dict[str, FileIdentity] = Field(min_length=4, max_length=60)
    implementation: dict[str, FileIdentity] = Field(min_length=3, max_length=3)
    environment: dict[Literal['python', 'pydantic'], str] = Field(min_length=2, max_length=2)
    record_ids: list[str] = Field(min_length=1, max_length=90)
