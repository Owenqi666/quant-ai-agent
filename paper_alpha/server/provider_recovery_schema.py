"""Independent recovery metadata; frozen provider v1 records are never rewritten."""
from __future__ import annotations

import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

SCHEMA = """
CREATE TABLE provider_recovery_actions(id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE provider_attempt_resolutions(attempt_id TEXT PRIMARY KEY REFERENCES proposal_attempts(id),proposal_id TEXT NOT NULL REFERENCES proposal_requests(id),attempt_digest TEXT NOT NULL,action_id TEXT NOT NULL REFERENCES provider_recovery_actions(id),payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE provider_attempt_origins(attempt_id TEXT PRIMARY KEY REFERENCES proposal_attempts(id),payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE provider_worker_activity(worker_id TEXT PRIMARY KEY,last_seen REAL NOT NULL,status TEXT NOT NULL,phase TEXT NOT NULL,config_digest TEXT,proposal_id TEXT,attempt_id TEXT);
"""


def pause_dispatch(connection, reason):
    """Every new pause gets an identity, including repeated equal reasons."""
    for key, value in (('proposal_dispatch_paused', reason),
                       ('provider_pause_generation', uuid.uuid4().hex)):
        connection.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


class RecoveryCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['reconcile', 'resume_new']
    plan_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=2000)


class ClosedStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProviderPermission(ClosedStatus):
    allow_real: bool
    allow_fixture: bool
    real_dispatch_enabled: bool
    scope: Literal['host_permission_only'] = 'host_permission_only'


class DispatchPause(ClosedStatus):
    paused: bool
    reason: Literal['interrupted_invocation', 'restored_workspace', 'other', 'none']
    generation: str | None
    unresolved_count: int | None = Field(ge=0)


class ProposalWorkerActivity(ClosedStatus):
    status: Literal['online', 'stale', 'stopped', 'not_observed']
    last_seen: str | None
    phase: Literal['idle', 'preparing', 'provider_execution', 'paused', 'fenced', 'stopped', 'unknown']
    config_digest: str | None
    proposal_id: str | None
    attempt_id: str | None
    freshness_seconds: int = 10


class ProviderRecoveryState(ClosedStatus):
    required: bool
    reason: Literal['paused', 'unresolved', 'none', 'integrity_unknown']
    action: Literal['offline_inspect', 'none']


class ProviderRuntimeStatus(ClosedStatus):
    schema_version: Literal[1] = 1
    permission: ProviderPermission
    dispatch_pause: DispatchPause
    proposal_worker: ProposalWorkerActivity
    recovery: ProviderRecoveryState
