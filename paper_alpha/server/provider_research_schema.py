"""Research provider v2 contracts; legacy provider v1 stays byte-for-byte frozen."""
from __future__ import annotations

from typing import Annotated, Literal
from functools import lru_cache
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel
from .provider_schema import DIGEST_PATTERN, ProposalBudget, ProposalAbstention
from .research_jobs_schema import ResearchDraft
from .research_sessions_schema import ResearchDecision

CONTRACT_ID = 'research_provider_v2'


from .provider_research_source_schema import (MaterialDestination, PaperSelectionSource,
    AcceptedMaterialSource, ResearchRevisionSource, ResearchSource)


class ResearchTarget(AuthorModel):
    proposal_id: str = Field(pattern=r'^proposal_[0-9a-f]{64}$')
    attempt_id: str = Field(pattern=r'^proposal_attempt_[0-9a-f]{64}$')
    response_digest: str = Field(pattern=DIGEST_PATTERN)
    candidate_id: str = Field(min_length=1, max_length=64)
    research_job_id: str | None = None
    result_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)


class ProviderCorrectionTarget(ResearchTarget):
    """Explicit additive target branch; old target bytes retain their shape.

    input_digest binds the observed research input, not the later prepared CLI
    bytes. The original slot separately pins the actual prepared_digest.
    """
    correction_version: Literal[1]
    policy_id: Literal['integral-window-representation-v1', 'equivalent-expression-representation-v2']
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    source_digest: str = Field(pattern=DIGEST_PATTERN)
    capability_digest: str = Field(pattern=DIGEST_PATTERN)
    diagnostics_digest: str = Field(pattern=DIGEST_PATTERN)
    parent_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    input_digest: str = Field(pattern=DIGEST_PATTERN)
    loop_id: str = Field(pattern=r'^research_loop_[0-9a-f]{64}$')
    action_sequence: int = Field(ge=1, le=24)
    effect_key: str = Field(pattern=r'^loop-effect-[0-9a-f]{64}$')

    @model_validator(mode='before')
    @classmethod
    def strict_correction_version(cls, value):
        if isinstance(value, dict) and type(value.get('correction_version')) is not int:
            raise ValueError('correction_version requires an integer')
        return value

    @model_validator(mode='after')
    def policy_reference(self):
        from .research_correction_policy import resolve_policy
        resolve_policy(self.policy_id, self.policy_digest)
        if self.research_job_id is not None or self.result_digest is not None:
            raise ValueError('Provider correction targets only an original rejected generation')
        return self


class ResearchProposalCreate(AuthorModel):
    schema_version: Literal[2] = 2
    contract_id: Literal['research_provider_v2'] = CONTRACT_ID
    operation: Literal['extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action']
    session_id: str = Field(min_length=1, max_length=128)
    expected_session_digest: str = Field(pattern=DIGEST_PATTERN)
    source: ResearchSource
    expected_source_digest: str = Field(pattern=DIGEST_PATTERN)
    provider_config_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(default='', max_length=4000)
    candidate_limit: int = Field(default=1, ge=1, le=3)
    target: ResearchTarget | ProviderCorrectionTarget | None = None
    budget: ProposalBudget = Field(default_factory=ProposalBudget)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @property
    def research_id(self):
        return getattr(self.source, 'research_id', None) if self.source.kind != 'accepted_material' else self.source.destination.research_id

    @property
    def base_revision_id(self):
        return getattr(self.source, 'base_revision_id', None) if self.source.kind != 'accepted_material' else self.source.destination.base_revision_id

    @property
    def purpose(self):
        return self.operation

    @model_validator(mode='after')
    def compatible_operation(self):
        if not self.idempotency_key.strip():
            raise ValueError('Idempotency key must not be blank')
        if (self.operation == 'extract_material') != (self.source.kind == 'paper_selection'):
            raise ValueError('Paper selection is exclusively a material extraction source')
        if (self.operation in {'repair_candidate', 'decide_next_action'}) != (self.target is not None):
            raise ValueError('Repair and decision require an exact prior target')
        if self.operation in {'repair_candidate', 'decide_next_action'} and self.source.kind != 'research_revision':
            raise ValueError('Initial repair and decision operate on a maintained research revision')
        if isinstance(self.target, ProviderCorrectionTarget) and (
                self.operation != 'repair_candidate' or self.candidate_limit != 1):
            raise ValueError('Versioned provider correction is one exact repair candidate')
        return self


@lru_cache(maxsize=1)
def output_model():
    # Material types are independent of the execution queue. The import is late
    # so schema migration need not import a new provider business implementation.
    from .paper_materials_schema import MaterialDraft

    class ResearchProviderOutput(AuthorModel):
        schema_version: Literal[2]
        contract_id: Literal['research_provider_v2']
        operation: Literal['extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action']
        proposal_id: str = Field(pattern=r'^proposal_[0-9a-f]{64}$')
        attempt_id: str = Field(pattern=r'^proposal_attempt_[0-9a-f]{64}$')
        context_digest: str = Field(pattern=DIGEST_PATTERN)
        outcome: Literal['material', 'draft', 'decision', 'abstain']
        material: MaterialDraft | None
        draft: ResearchDraft | None
        decision: ResearchDecision | None
        abstention: ProposalAbstention | None

        @model_validator(mode='after')
        def one_branch(self):
            expected = {'extract_material': 'material', 'generate_candidates': 'draft',
                        'repair_candidate': 'draft', 'decide_next_action': 'decision'}[self.operation]
            if self.outcome not in {expected, 'abstain'}:
                raise ValueError('Outcome differs from the requested operation')
            branch = 'abstention' if self.outcome == 'abstain' else self.outcome
            if any((getattr(self, name) is not None) != (name == branch)
                   for name in ('material', 'draft', 'decision', 'abstention')):
                raise ValueError('Output must contain exactly its declared branch')
            if self.draft and len(self.draft.candidates) > 3:
                raise ValueError('At most three candidate definitions are permitted')
            return self

    return ResearchProviderOutput
