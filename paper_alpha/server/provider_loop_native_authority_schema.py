"""Closed host policy for the versioned, original-session Native authority.

Schema constants have no import-time I/O. Slots link to ResearchSessions; they
are not a second queue or a refundable budget. HTTP never creates this policy.
Schema27 migration and backup explicitly allow only these exact triggers.
Immutable local helpers and original session/attempt inventory checks also
enforce permanent charge and revocation; grants and observations have no
application update/delete operation.
"""
from typing import Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel

AUTHORITY_ID = 'research_loop_native_v1'
OPERATIONS = {
    'extract_material': 'paper_selection',
    'generate_candidates': ('accepted_material', 'research_revision'),
    'repair_candidate': 'research_revision',
    'decide_next_action': 'research_revision',
}

SCHEMA = """
CREATE TABLE provider_loop_native_grants(id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE provider_loop_native_sessions(session_id TEXT PRIMARY KEY REFERENCES research_sessions(id),grant_id TEXT NOT NULL REFERENCES provider_loop_native_grants(id),payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE provider_loop_native_slots(grant_id TEXT NOT NULL REFERENCES provider_loop_native_grants(id),call_id TEXT NOT NULL,effect_key TEXT NOT NULL,session_id TEXT REFERENCES research_sessions(id),reservation_token TEXT UNIQUE REFERENCES research_session_reservations(token),proposal_id TEXT UNIQUE REFERENCES proposal_requests(id),attempt_id TEXT UNIQUE,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(grant_id,call_id),UNIQUE(grant_id,effect_key));
CREATE TABLE provider_loop_native_revocations(grant_id TEXT PRIMARY KEY REFERENCES provider_loop_native_grants(id),payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE provider_loop_native_observations(attempt_id TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>0),payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(attempt_id,sequence));
CREATE TRIGGER loop_native_grants_no_update BEFORE UPDATE ON provider_loop_native_grants BEGIN SELECT RAISE(ABORT,'immutable Native grant'); END;
CREATE TRIGGER loop_native_grants_no_delete BEFORE DELETE ON provider_loop_native_grants BEGIN SELECT RAISE(ABORT,'immutable Native grant'); END;
CREATE TRIGGER loop_native_slots_no_delete BEFORE DELETE ON provider_loop_native_slots BEGIN SELECT RAISE(ABORT,'permanent Native slot'); END;
CREATE TRIGGER loop_native_observations_no_update BEFORE UPDATE ON provider_loop_native_observations BEGIN SELECT RAISE(ABORT,'immutable Native observation'); END;
CREATE TRIGGER loop_native_observations_no_delete BEFORE DELETE ON provider_loop_native_observations BEGIN SELECT RAISE(ABORT,'permanent Native observation'); END;
CREATE TRIGGER loop_native_revocations_no_update BEFORE UPDATE ON provider_loop_native_revocations BEGIN SELECT RAISE(ABORT,'immutable Native revocation'); END;
CREATE TRIGGER loop_native_revocations_no_delete BEFORE DELETE ON provider_loop_native_revocations BEGIN SELECT RAISE(ABORT,'permanent Native revocation'); END;
"""

REQUIRED_COLUMNS = {
    'provider_loop_native_grants': {'id', 'idempotency_key', 'payload', 'digest'},
    'provider_loop_native_sessions': {'session_id', 'grant_id', 'payload', 'digest'},
    'provider_loop_native_slots': {'grant_id', 'call_id', 'effect_key', 'session_id', 'reservation_token', 'proposal_id', 'attempt_id', 'payload', 'digest'},
    'provider_loop_native_revocations': {'grant_id', 'payload', 'digest'},
    'provider_loop_native_observations': {'attempt_id', 'sequence', 'payload', 'digest'},
}

class NativeCallSlot(AuthorModel):
    call_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    effect_key: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    operation: Literal['extract_material', 'generate_candidates', 'repair_candidate', 'decide_next_action']
    source_kind: Literal['paper_selection', 'accepted_material', 'research_revision']
    root_source_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    adopted_from_call_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    instruction_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    target_from_call_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_.-]{1,64}$')
    candidate_limit: int = Field(default=1, ge=1, le=2)

    @model_validator(mode='after')
    def valid_pair(self):
        allowed = OPERATIONS[self.operation]
        if self.source_kind not in (allowed if isinstance(allowed, tuple) else (allowed,)):
            raise ValueError('Call operation and source branch differ')
        if (self.root_source_digest is None) == (self.adopted_from_call_id is None):
            raise ValueError('Freeze an exact root source or exact adopted extraction call')
        if self.adopted_from_call_id is not None and self.source_kind != 'accepted_material':
            raise ValueError('Only adopted material may follow a frozen extraction slot')
        if self.target_from_call_id is not None and self.operation not in {'repair_candidate','decide_next_action'}:
            raise ValueError('Only repair or decide may name an earlier target call')
        return self

class NativeCorrectionCallSlot(NativeCallSlot):
    """Required-version slot; an old slot cannot silently permit a new policy."""
    correction_version: Literal[1]
    operation: Literal['repair_candidate']
    source_kind: Literal['research_revision']
    candidate_limit: Literal[1] = 1
    policy_id: Literal['integral-window-representation-v1', 'equivalent-expression-representation-v2']
    policy_digest: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='before')
    @classmethod
    def strict_correction_version(cls, value):
        if isinstance(value, dict) and type(value.get('correction_version')) is not int:
            raise ValueError('correction_version requires an integer')
        return value

    @model_validator(mode='after')
    def correction_scope(self):
        from .research_correction_policy import resolve_policy
        resolve_policy(self.policy_id, self.policy_digest)
        if self.target_from_call_id is None or self.instruction_digest is None:
            raise ValueError('A correction slot freezes its exact parent call and instruction')
        return self

class LoopNativeGrantCreate(AuthorModel):
    program_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    plan_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    provider_config_id: str = Field(min_length=1, max_length=128)
    slots: list[NativeCallSlot | NativeCorrectionCallSlot] = Field(min_length=1, max_length=11)
    max_seconds: int = Field(default=1800, ge=1, le=7200)
    max_proposal_seconds: int = Field(default=240, ge=1, le=240)
    authorization_kind: Literal['fixture', 'bounded_qualification'] = 'fixture'
    human_authorization_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')

    @model_validator(mode='after')
    def unique_slots(self):
        if (len({s.call_id for s in self.slots}) != len(self.slots)
                or len({s.effect_key for s in self.slots}) != len(self.slots)):
            raise ValueError('Call IDs and effect keys must be unique')
        by_id = {s.call_id: s for s in self.slots}
        for slot in self.slots:
            if slot.adopted_from_call_id is not None:
                parent = by_id.get(slot.adopted_from_call_id)
                if parent is None or parent.operation != 'extract_material':
                    raise ValueError('Generation must name this program exact extraction slot')
            if slot.target_from_call_id is not None:
                parent = by_id.get(slot.target_from_call_id)
                if parent is None or parent.operation not in {'generate_candidates','repair_candidate'}:
                    raise ValueError('A target must name an exact earlier candidate call')
        if self.authorization_kind != 'fixture' and self.human_authorization_digest is None:
            raise ValueError('A real local grant requires its scoped human authorization receipt')
        if self.authorization_kind != 'fixture' and any(s.instruction_digest is None or
                (s.operation in {'repair_candidate','decide_next_action'} and s.target_from_call_id is None) for s in self.slots):
            raise ValueError('Freeze every real instruction and exact target call before authorization')
        return self
