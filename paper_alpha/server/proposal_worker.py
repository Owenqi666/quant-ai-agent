"""Optional proposal dispatch with process fencing; no model call on import.

This worker never claims deterministic computation jobs or automatically retries
an uncertain invocation. Actual provider selection belongs to the host caller.
"""
from __future__ import annotations

import asyncio
import argparse
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import signal
import time
import uuid

from .maintenance import workspace_lease
from .provider_contract_registry import request_handler
from .provider_schema import (PreparedProposalRequest, ProviderCallControl,
                              ProviderConfig, ProviderReceipt)
from .proposal_requests import ProposalServiceError, _error
from .service import now, Store, REPO
from ..storage import digest
from .provider_runtime_status import heartbeat
from .provider_supervision_journal import journal_scope

LOG = logging.getLogger('paper_alpha.proposal_worker')


class ProposalWorker:
    def __init__(self, requests, *, worker_id=None):
        self.requests = requests
        self.worker_id = worker_id or 'proposal-worker-' + str(uuid.uuid4())
        self._last_activity = (None, 0.0)

    def _activity(self, phase, *, registry=None, proposal_id=None, attempt_id=None, stopped=False):
        config_digest = registry.startup_digest() if registry is not None else None
        key = (phase, config_digest, proposal_id, attempt_id, stopped)
        if key != self._last_activity[0] or time.monotonic() - self._last_activity[1] >= 1:
            heartbeat(self.requests.store, self.worker_id, phase=phase, config_digest=config_digest,
                      proposal_id=proposal_id, attempt_id=attempt_id, stopped=stopped)
            self._last_activity = (key, time.monotonic())

    async def run_once(self, provider=None, *, registry=None, stop_event=None) -> bool:
        """Process one request, preserving explicit-provider test compatibility.

        Production-style dispatch always supplies a registry. Its config digest
        filter ensures a request is never claimed for another configuration.
        """
        if provider is None and registry is None:
            raise ValueError('A provider registry or explicit provider is required')
        if stop_event is not None and stop_event.is_set():
            return False
        service = self.requests
        with workspace_lease(service.store.root) as lease_fd, service._lock('worker') as worker_fd:
            self._activity('paused' if service.store._read("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'") else 'idle', registry=registry)
            service.recover_interrupted()
            claimed = service.claim(self.worker_id, config_digests=registry.dispatch_config_digests()) if registry is not None else service.claim(self.worker_id)
            if claimed is None:
                if service.store._read("SELECT 1 FROM settings WHERE key='proposal_dispatch_paused'"):
                    self._activity('paused', registry=registry)
                return False
            value, attempt = claimed['proposal'], claimed['attempt']
            self._activity('preparing', registry=registry, proposal_id=value['id'], attempt_id=attempt['id'])
            handler = request_handler(value['request'])
            request = handler.parse_request(value['request'])
            config = ProviderConfig.model_validate(value['config'])
            remaining = max(0.0, (datetime.fromisoformat(value['deadline_at']) - datetime.now(timezone.utc)).total_seconds())
            artifact_dir = service._folder(value['id']) / 'attempts' / attempt['id']
            artifact_dir.mkdir(parents=True, exist_ok=True)
            control = ProviderCallControl(deadline_monotonic=time.monotonic() + remaining,
                artifact_dir=artifact_dir / 'transport', pass_fds=(lease_fd, worker_fd))
            prepared = PreparedProposalRequest(proposal_id=value['id'], attempt_id=attempt['id'],
                context_digest=value['context_digest'],
                input_text=handler.build_prompt(request, value['context'], value['id'], attempt['id']),
                output_schema=handler.output_schema(value['context']), config=config, budget=request.budget)
            prepared_value = service.register_prepared(value['id'], attempt['id'], attempt['fencing_token'], {
                'proposal_id': value['id'], 'attempt_id': attempt['id'], 'context_digest': value['context_digest'],
                'input_text': prepared.input_text, 'output_schema': prepared.output_schema,
                'provider_config': config.model_dump(), 'budget': request.budget.model_dump()})
            response, error, receipt = None, None, None
            entered_provider = False
            monitor_error = None

            async def monitor_loop():
                while True:
                    self._activity('provider_execution', registry=registry, proposal_id=value['id'], attempt_id=attempt['id'])
                    stopping = stop_event is not None and stop_event.is_set()
                    if request.schema_version == 2:
                        from .research_session_execution import proposal_session_cancelled
                        try:
                            stopping = stopping or proposal_session_cancelled(service.store, value)
                            authority = service.research_authority
                            if authority is not None and hasattr(authority, 'cancelled'):
                                stopping = stopping or authority.cancelled(value)
                        except (ValueError, OSError):
                            # A parent which cannot be verified grants no further
                            # work; retain the child cancellation and receipt.
                            stopping = True
                    if stopping:
                        control.cancel_event.set()
                        try:
                            service.cancel(value['id'])
                        except ProposalServiceError as exc:
                            if exc.code != 'PROPOSAL_BUSY':
                                raise
                            await asyncio.sleep(.05)
                            continue
                    if stopping or service.get(value['id'])['status'] == 'cancelling' or time.monotonic() >= control.deadline_monotonic:
                        control.cancel_event.set()
                        return
                    await asyncio.sleep(.05)

            async def monitor_cancel():
                nonlocal monitor_error
                try:
                    await monitor_loop()
                except Exception as exc:
                    # Losing the cancellation observer cannot grant continued
                    # execution. The adapter must still report actual cleanup.
                    control.cancel_event.set()
                    monitor_error = _error('CANCELLATION_MONITOR_FAILED', service.store._safe_error(exc))
                    raise

            monitor = asyncio.create_task(monitor_cancel())
            try:
                if registry is not None:
                    current = registry.ensure_dispatchable(config.config_id)
                    if current != config:
                        raise ProposalServiceError('Provider differs from the frozen configuration.', code='CONFIG_CHANGED')
                    authority = service.research_authority
                    if request.schema_version == 2:
                        from .provider_authority_router import ResearchAuthorityRouter
                        if type(authority) is ResearchAuthorityRouter:
                            authority = authority.leaf_for_session(request.session_id)
                        provider = registry.create_provider(config.config_id, contract_id=request.contract_id,
                                                            research_authority=authority)
                    else:
                        provider = registry.create_provider(config.config_id)
                configured = getattr(provider, 'config', config)
                if hasattr(configured, 'model_dump') and configured.model_dump() != config.model_dump():
                    raise ProposalServiceError('Provider differs from frozen configuration', code='CONFIG_CHANGED')
                if request.schema_version == 2:
                    from .research_session_execution import proposal_dispatch_guard
                    remaining_session = proposal_dispatch_guard(service.store, value['id'], proposals=service)
                    if remaining_session <= 0:
                        raise ProposalServiceError('Session deadline elapsed', code='SESSION_EXPIRED')
                    control.deadline_monotonic = min(control.deadline_monotonic, time.monotonic() + remaining_session)
                prepared_attempt = prepared_value['attempts'][-1]
                if request.schema_version == 2 and config.mode != 'fixture':
                    # This checks the original charged slot and frozen bytes;
                    # the native adapter consumes its dispatch intent later.
                    if service.research_authority is None:
                        raise ProposalServiceError('Research provider v2 has no explicit host authority',
                                                   code='RESEARCH_V2_REAL_UNQUALIFIED')
                    service.research_authority.authorize_prepared(prepared_value, prepared_attempt,
                                                                  prepared_attempt['prepared_digest'])
                    # Qualification reads may consume the remaining parent time.
                    remaining_session = proposal_dispatch_guard(service.store, value['id'], proposals=service)
                    control.deadline_monotonic = min(control.deadline_monotonic, time.monotonic() + remaining_session)
                binding = {'workspace_identity': service.store._read("SELECT value FROM settings WHERE key='provider_workspace_identity'")[0]['value'],
                           'proposal_id': value['id'], 'attempt_id': attempt['id'], 'fencing_token': attempt['fencing_token'],
                           'prepared_digest': prepared_attempt['prepared_digest'], 'config_digest': digest(value['config']),
                           'attempt_started_digest': digest(prepared_attempt)}
                with journal_scope(artifact_dir / 'supervision', binding):
                    entered_provider = True
                    result = await provider.propose(prepared, control)
                receipt = result.receipt
                if stop_event is not None and stop_event.is_set():
                    service.cancel(value['id'])
                if result.error is not None:
                    error = result.error.model_dump()
                if result.raw_response is not None:
                    raw = result.raw_response
                    if len(raw) > request.budget.max_output_bytes:
                        error = _error('OUTPUT_TOO_LARGE', 'Provider response exceeds its byte bound.')
                    else:
                        # Keep the exact bytes even if strict acceptance fails.
                        path = artifact_dir / 'response.raw.json'
                        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(descriptor, 'wb') as stream:
                            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                        if receipt.response_sha256 != hashlib.sha256(raw).hexdigest():
                            error = _error('ARTIFACT_INTEGRITY', 'Raw response hash differs from the provider receipt.')
                        if error is None:
                            response = handler.parse_output(raw, request=request, context=value['context'], proposal_id=value['id'], attempt_id=attempt['id'],
                                context_digest=value['context_digest'], candidate_limit=request.candidate_limit,
                                max_bytes=request.budget.max_output_bytes).model_dump()
            except Exception as exc:
                error = _error('OUTCOME_UNKNOWN' if entered_provider and receipt is None else getattr(exc, 'code', 'PROVIDER_FAILED'), service.store._safe_error(exc))
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if monitor_error is not None and error is None:
                error = monitor_error
            if receipt is None and not entered_provider:
                receipt = ProviderReceipt(proposal_id=value['id'], attempt_id=attempt['id'],
                    started_at=attempt['started_at'], finished_at=now(), elapsed_seconds=max(0.0, remaining - max(0.0, control.deadline_monotonic - time.monotonic())),
                    requested_model=config.model, fixture=config.mode == 'fixture')
            service.finish(value['id'], attempt['id'], attempt['fencing_token'],
                           response=response, receipt=receipt, error=error)
            self._activity('idle', registry=registry)
            return True

    async def run_forever(self, registry, *, stop_event=None, poll_seconds=.2):
        """Poll the independent proposal queue until a host stop signal arrives."""
        if not .01 <= poll_seconds <= 5:
            raise ValueError('poll_seconds must be in 0.01..5')
        stopping = stop_event or asyncio.Event()
        with workspace_lease(self.requests.store.root):
            try:
                while not stopping.is_set():
                    try:
                        processed = await self.run_once(registry=registry, stop_event=stopping)
                    except ProposalServiceError as exc:
                        if exc.code != 'PROPOSAL_BUSY':
                            raise
                        self._activity('fenced', registry=registry)
                        processed = False
                    if processed:
                        continue
                    try:
                        await asyncio.wait_for(stopping.wait(), timeout=poll_seconds)
                    except TimeoutError:
                        pass
            finally:
                self._activity('stopped', registry=registry, stopped=True)


async def run_once(requests, provider):
    """Convenient one-shot entry point; no default executable/model is selected."""
    return await ProposalWorker(requests).run_once(provider)


def main(argv=None):
    from .provider_runtime import load_provider_registry
    from .proposal_requests import ProposalRequests
    parser = argparse.ArgumentParser(description='Run the optional local proposal worker')
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('PAPER_ALPHA_HOME', REPO / 'var/workbench')))
    parser.add_argument('--provider-config', type=Path)
    parser.add_argument('--allow-fixture-provider', action='store_true')
    parser.add_argument('--allow-real-provider', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=.2)
    args = parser.parse_args(argv)
    if not .01 <= args.poll_seconds <= 5:
        parser.error('poll-seconds must be in 0.01..5')
    try:
        registry = load_provider_registry(args.provider_config, allow_fixture=args.allow_fixture_provider,
                                          allow_real=args.allow_real_provider)
        if not registry.dispatch_config_digests():
            LOG.warning('No dispatchable proposal provider; worker will not start.')
            return 0
        store = Store(args.home)
        worker = ProposalWorker(registry.proposal_requests(store))

        async def serve():
            stopping = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stopping.set)
            try:
                await worker.run_forever(registry, stop_event=stopping, poll_seconds=args.poll_seconds)
            finally:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(signum)

        asyncio.run(serve())
        return 0
    except (ValueError, OSError) as exc:
        LOG.error('Proposal worker stopped: %s', str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
