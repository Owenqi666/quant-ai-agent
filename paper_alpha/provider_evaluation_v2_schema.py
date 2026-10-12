"""Closed execution plan for the three-attempt G4 development evaluation."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .author_panel_schema import AuthorModel
from .server.provider_schema import ProviderConfig
from .server.provider_research_source_schema import ResearchRevisionSource

Hash = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
CaseId = Literal['qualification-generate', 'alpha101-supported', 'alpha005-missing-vwap']


class EvaluationInput(AuthorModel):
    source: ResearchRevisionSource
    source_digest: Hash
    task_digest: Hash
    paper_sha256: Hash
    dataset_sha256: Hash
    data_file_sha256: Hash
    metadata_file_sha256: Hash
    evidence_ids: list[str] = Field(min_length=1, max_length=1)
    hypothesis_ids: list[str] = Field(min_length=1, max_length=1)


class EvaluationCase(AuthorModel):
    id: CaseId
    role: Literal['qualification', 'positive', 'negative']
    input: EvaluationInput
    operation: Literal['generate_candidates'] = 'generate_candidates'
    max_attempts: Literal[1] = 1
    candidate_limit: Literal[1] = 1
    allow_experiment: bool


class EvaluationBudget(AuthorModel):
    max_real_provider_attempts: Literal[3] = 3
    max_engine_runs: Literal[2] = 2
    max_seconds: Literal[900] = 900
    max_attempt_seconds: Literal[120] = 120
    # Existing per-artifact limits apply; no unsupported total byte cap is claimed.
    max_artifact_bytes: None = None
    hard_backend_model_call_cap_supported: Literal[False] = False
    max_currency_cost: None = None


class NativeIdentity(AuthorModel):
    contract_id: Literal['research_provider_v2']
    profile_digest: Hash
    config_digest: Hash
    read_policy_digest: Hash
    context_policy_version: str
    source_digests: dict[str, Hash] = Field(min_length=1, max_length=100)


class ExecutionPlan(AuthorModel):
    schema_version: Literal[1] = 1
    kind: Literal['provider_development_execution_plan'] = 'provider_development_execution_plan'
    suite_id: Literal['provider_dev_v2'] = 'provider_dev_v2'
    contract_id: Literal['research_provider_v2'] = 'research_provider_v2'
    operation: Literal['generate_candidates'] = 'generate_candidates'
    mode: Literal['provider_one_shot'] = 'provider_one_shot'
    repetitions: Literal[1] = 1
    id: str = Field(pattern=r'^provider_eval_plan_[a-f0-9]{64}$')
    digest: Hash
    config: ProviderConfig
    native_identity: NativeIdentity
    suite_digest: Hash
    implementation: dict[str, Hash] = Field(min_length=1, max_length=1000)
    environment: dict[str, str] = Field(min_length=1, max_length=10)
    cases: list[EvaluationCase] = Field(min_length=3, max_length=3)
    baseline: EvaluationInput
    budget: EvaluationBudget
    semantic_fidelity: Literal['unverified'] = 'unverified'
    final_test_execution: Literal['locked'] = 'locked'

    @model_validator(mode='after')
    def fixed_scope(self):
        expected = [('qualification-generate', 'qualification', False),
                    ('alpha101-supported', 'positive', True),
                    ('alpha005-missing-vwap', 'negative', False)]
        if [(c.id, c.role, c.allow_experiment) for c in self.cases] != expected:
            raise ValueError('The three predeclared cases and order are fixed')
        c = self.config
        if (c.mode != 'codex_cli' or not c.enabled or c.model != 'gpt-5.5'
                or c.reasoning_effort != 'low' or c.expected_cli_version != '0.145.0'):
            raise ValueError('Only the pinned explicitly enabled native profile is in scope')
        return self


class OutputPolicyIdentity(AuthorModel):
    policy_id: Literal['research_missing_fields_v1']
    resource_sha256: Hash
    instruction_sha256: Hash


class ExecutionPlanV2(ExecutionPlan):
    """Opt-in output policy; the historical v1 dump gains no default fields."""
    schema_version: Literal[2] = 2
    suite_id: Literal['provider_dev_v3'] = 'provider_dev_v3'
    output_policy: OutputPolicyIdentity


def parse_execution_plan(value):
    raw = value.model_dump() if hasattr(value, 'model_dump') else value
    if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int:
        raise ValueError('Execution plan requires an explicit integer schema version')
    model = {1: ExecutionPlan, 2: ExecutionPlanV2}.get(raw['schema_version'])
    if model is None:
        raise ValueError('Unsupported execution plan schema version')
    return model.model_validate(raw)
