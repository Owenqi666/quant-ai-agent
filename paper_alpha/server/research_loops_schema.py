"""Explicit host policy for one bounded, fixture-only research loop."""
from typing import Literal
from pydantic import Field, create_model, model_validator
from ..author_panel_schema import AuthorModel
from .provider_research_schema import ResearchProposalCreate, ResearchTarget
from .research_session_experiments_schema import ExperimentBudget


# Keep the original v1 nested wire schema and digest. Its initial generation
# cannot carry a repair target; only the explicitly versioned v2 loop exposes
# the additive Provider correction target union.
OriginalLoopProposalCreate = create_model('ResearchProposalCreate',
    __base__=ResearchProposalCreate, target=(ResearchTarget | None, None))


class LoopCreate(AuthorModel):
    schema_version: Literal[1] = 1
    initial_proposal: OriginalLoopProposalCreate
    max_repairs: int = Field(default=1, ge=0, le=2)
    allow_experiment: bool = False
    experiment_budget: ExperimentBudget | None = None
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')

    @model_validator(mode='after')
    def scope(self):
        initial = self.initial_proposal
        if (initial.operation != 'generate_candidates' or initial.source.kind != 'research_revision'
                or initial.candidate_limit != 1):
            raise ValueError('A loop begins with one generated candidate on a maintained research revision')
        if self.allow_experiment != (self.experiment_budget is not None):
            raise ValueError('Explicit experiment permission and its job budget must be supplied together')
        return self


SCHEMA = """
CREATE TABLE research_loops(id TEXT PRIMARY KEY,session_id TEXT NOT NULL UNIQUE REFERENCES research_sessions(id),idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,payload TEXT NOT NULL,payload_digest TEXT NOT NULL,created_at TEXT NOT NULL,action_count INTEGER NOT NULL,ledger_digest TEXT NOT NULL);
CREATE TABLE research_loop_actions(loop_id TEXT NOT NULL REFERENCES research_loops(id),sequence INTEGER NOT NULL,idempotency_key TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(loop_id,sequence),UNIQUE(loop_id,idempotency_key));
"""


# Additive request contract only. LoopCreate and the existing DDL above remain
# unchanged; runtime/API integrations still explicitly use their original v1.
from .research_correction_schema import PolicyId, Sha256
from .research_correction_policy import resolve_policy


class LoopCreateV2(LoopCreate):
    """Explicit policy-bound request; parsing does not authorize execution."""
    schema_version: Literal[2] = Field(...)
    initial_proposal: ResearchProposalCreate
    correction_policy_id: PolicyId
    correction_policy_digest: Sha256
    correction_mode: Literal['deterministic', 'provider_optional'] = 'deterministic'

    @model_validator(mode='after')
    def correction_policy(self):
        resolve_policy(self.correction_policy_id, self.correction_policy_digest)
        if self.correction_mode == 'provider_optional' and self.max_repairs != 1:
            raise ValueError('Current provider correction qualification permits exactly one repair slot')
        return self


def parse_loop_create(value):
    """Closed v1/v2 request dispatcher; omitted version keeps original v1.

    No rule pass, database, permission grant, reservation or dispatch occurs.
    New v2 callers must supply their explicit integer discriminator and trusted
    policy reference. Unsupported types/versions never fall back to v1.
    """
    if type(value) is not dict:
        raise ValueError('Loop request must be a JSON object')
    version = value.get('schema_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Loop schema_version requires supported integer 1 or 2')
    return (LoopCreate if version == 1 else LoopCreateV2).model_validate(value)
