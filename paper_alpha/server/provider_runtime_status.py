"""Persisted proposal-worker activity, separate from host dispatch permission."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import re
import time

from fastapi import APIRouter
from .db import connect, transaction
from .provider_recovery import unresolved_interrupted
from .provider_recovery_schema import ProviderRuntimeStatus
from .proposal_requests import ProposalRequests, ProposalServiceError

FRESHNESS_SECONDS = 10


def heartbeat(store, worker_id, *, phase, config_digest=None, proposal_id=None, attempt_id=None, stopped=False):
    with transaction(store.db_path) as connection:
        connection.execute('INSERT INTO provider_worker_activity VALUES (?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET '
            'last_seen=excluded.last_seen,status=excluded.status,phase=excluded.phase,config_digest=excluded.config_digest,'
            'proposal_id=excluded.proposal_id,attempt_id=excluded.attempt_id',
            (worker_id, time.time(), 'stopped' if stopped else 'running', phase, config_digest, proposal_id, attempt_id))


def runtime_status(store, registry=None):
    raw = registry.public_status() if registry is not None else {}
    permission = {'allow_real': raw.get('allow_real', False), 'allow_fixture': raw.get('allow_fixture', False),
                  'real_dispatch_enabled': raw.get('real_dispatch_enabled', False), 'scope': 'host_permission_only'}
    with closing(connect(store.db_path)) as connection:
        connection.execute('BEGIN')
        settings = dict(connection.execute('SELECT key,value FROM settings'))
        workers = connection.execute('SELECT * FROM provider_worker_activity ORDER BY last_seen DESC LIMIT 1').fetchall()
        unknown = False
        try:
            unresolved = len(unresolved_interrupted(connection, ProposalRequests(store, configs={})))
        except (ProposalServiceError, ValueError, OSError):
            unresolved, unknown = 0, True
        # An active attempt whose worker was lost is still actionable before
        # the restarted worker records its interrupted terminal state.
        active_rows = connection.execute("SELECT id FROM proposal_requests WHERE status IN ('running','validating','cancelling') LIMIT 1001").fetchall()
        active = 0
        if len(active_rows) > 1000:
            unknown = True
        else:
            for item in active_rows:
                try:
                    value = ProposalRequests(store, configs={})._load(connection, item['id'])
                    attempt = value['attempts'][-1]
                    owner = connection.execute('SELECT * FROM provider_worker_activity WHERE worker_id=?', (attempt['worker_id'],)).fetchone()
                    owns = (owner is not None and owner['status'] == 'running' and
                            owner['phase'] in {'preparing', 'provider_execution'} and
                            0 <= time.time() - owner['last_seen'] < FRESHNESS_SECONDS and
                            owner['proposal_id'] == value['id'] and owner['attempt_id'] == attempt['id'])
                    active += not owns
                    if owns:
                        workers = [owner]
                except (ProposalServiceError, ValueError, OSError):
                    unknown = True
    worker = {'status': 'not_observed', 'last_seen': None, 'phase': 'unknown', 'config_digest': None,
              'proposal_id': None, 'attempt_id': None, 'freshness_seconds': FRESHNESS_SECONDS}
    if workers:
        row = workers[0]
        age = time.time() - row['last_seen']
        worker.update(status='stopped' if row['status'] == 'stopped' else 'online' if 0 <= age < FRESHNESS_SECONDS else 'stale',
            last_seen=datetime.fromtimestamp(row['last_seen'], timezone.utc).isoformat(),
            phase=row['phase'] if row['phase'] in {'idle', 'preparing', 'provider_execution', 'paused', 'fenced', 'stopped'} else 'unknown',
            config_digest=row['config_digest'] if re.fullmatch('[0-9a-f]{64}', row['config_digest'] or '') else None,
            proposal_id=row['proposal_id'] if re.fullmatch('proposal_[0-9a-f]{64}', row['proposal_id'] or '') else None,
            attempt_id=row['attempt_id'] if re.fullmatch('proposal_attempt_[0-9a-f]{64}', row['attempt_id'] or '') else None)
    reason = settings.get('proposal_dispatch_paused')
    unresolved += active
    required = bool(reason or unresolved or unknown)
    generation = settings.get('provider_pause_generation')
    return ProviderRuntimeStatus.model_validate({'schema_version': 1, 'permission': permission,
        'dispatch_pause': {'paused': reason is not None, 'reason': reason if reason in {'interrupted_invocation', 'restored_workspace'} else 'other' if reason else 'none',
                           'generation': generation if re.fullmatch('[0-9a-f]{32}', generation or '') else None,
                           'unresolved_count': None if unknown else unresolved},
        'proposal_worker': worker, 'recovery': {'required': required,
            'reason': 'integrity_unknown' if unknown else 'paused' if reason else 'unresolved' if unresolved else 'none',
            'action': 'offline_inspect' if required else 'none'}}).model_dump()


def build_router(store, registry):
    router = APIRouter(prefix='/api/provider-runtime', tags=['provider runtime'])

    @router.get('/status', response_model=ProviderRuntimeStatus)
    def status():
        return runtime_status(store, registry)

    return router
