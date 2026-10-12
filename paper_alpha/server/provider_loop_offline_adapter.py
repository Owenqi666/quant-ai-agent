"""Fixed in-process fixture adapter in the existing ProposalWorker boundary.

No subprocess, network, CLI, caller callback or model-provided proof is used.
Provider dispatches here are actual fixture queue attempts, not real models.
"""
from copy import deepcopy
import hashlib
import time
from .provider_contract import canonical_json
from .provider_loop_host_proof import HostLoopProofResolver, initialize_host_anchor, HostProofError
from .provider_schema import ProviderAttemptResult, ProviderError, ProviderReceipt
from .service import now


class HostObservedOfflineAdapter:
    def __init__(self, proposals, config_id, *, fault='none'):
        if fault not in {'none', 'unknown', 'cancelled', 'wrong_repair'}:
            raise ValueError('Offline fixture fault is code owned')
        config = proposals.configs[config_id]
        if config.mode != 'fixture' or not config.enabled:
            raise ValueError('Offline adapter never accepts a real model configuration')
        self.config = config.model_copy(deep=True)
        self.proposals, self.fault = proposals, fault
        initialize_host_anchor(proposals)
        self.resolver = HostLoopProofResolver(proposals)
        self.fixture_calls, self.proofs = 0, []

    async def propose(self, request, control):
        started = now(); tick = time.monotonic()
        try:
            proof = self.resolver.resolve_prepared(request)
        except (HostProofError, ValueError, OSError) as exc:
            receipt = ProviderReceipt(proposal_id=request.proposal_id, attempt_id=request.attempt_id,
                started_at=started, finished_at=now(), elapsed_seconds=time.monotonic()-tick,
                fixture=True, invocation_started=False, process_stopped=True)
            return ProviderAttemptResult(None, ProviderError(code=getattr(exc, 'code', 'HOST_PROOF_INVALID'),
                message='Offline preflight failed before any fixture invocation'), receipt)
        if control.cancel_event.is_set() or time.monotonic() >= control.deadline_monotonic:
            receipt = ProviderReceipt(proposal_id=request.proposal_id, attempt_id=request.attempt_id,
                started_at=started, finished_at=now(), elapsed_seconds=time.monotonic()-tick,
                fixture=True, invocation_started=False, process_stopped=True)
            return ProviderAttemptResult(None, ProviderError(code='CANCELLED', message='Offline fixture was cancelled before invocation'), receipt)
        self.resolver.publish(proof); self.proofs.append(proof.model_dump()); self.fixture_calls += 1
        if self.fault in {'unknown', 'cancelled'}:
            receipt = ProviderReceipt(proposal_id=request.proposal_id, attempt_id=request.attempt_id,
                started_at=started, finished_at=now(), elapsed_seconds=time.monotonic()-tick,
                fixture=True, invocation_started=True, process_stopped=self.fault != 'unknown')
            return ProviderAttemptResult(None, ProviderError(code='OUTCOME_UNKNOWN' if self.fault == 'unknown' else 'CANCELLED',
                message='Fixed offline fault observation; no real model execution'), receipt)
        proposal = self.proposals.get(request.proposal_id); context = proposal['context']; operation = proposal['request']['operation']
        exposed = context.get('research_context', context)
        packet = {'schema_version': 2, 'contract_id': 'research_provider_v2', 'operation': operation,
            'proposal_id': request.proposal_id, 'attempt_id': request.attempt_id, 'context_digest': request.context_digest,
            'outcome': 'decision' if operation == 'decide_next_action' else 'draft',
            'material': None, 'draft': None, 'decision': None, 'abstention': None}
        if operation == 'decide_next_action':
            result = context['results'][0]
            packet['decision'] = {'action': 'stop', 'reason': 'Fixed offline fixture completed; independent research quality is unverified.',
                'candidate_id': result['candidate_id'], 'target_digest': result['result_digest'], 'information_gaps': []}
        else:
            if 'h-alpha006' not in {h['id'] for h in exposed['hypotheses']}:
                raise ValueError('Fixed offline demo requires the maintained Alpha006 hypothesis')
            expression = '-1 * ts_corr(open, volume, 10.0)'
            if operation == 'repair_candidate':
                expression = '-1 * ts_corr(open, volume, 20)' if self.fault == 'wrong_repair' else '-1 * ts_corr(open, volume, 10)'
            packet['draft'] = {'evidence': deepcopy(exposed['evidence']), 'hypotheses': deepcopy(exposed['hypotheses']),
                'candidates': [{'id': 'alpha006', 'hypothesis_id': 'h-alpha006', 'expression': expression,
                    'origin': 'model_conjecture', 'changes': ['Fixed offline representation-error fixture; no paper-original research claim']}]}
        raw = canonical_json(packet).encode()
        if len(raw) > request.budget.max_output_bytes:
            raise ValueError('Fixed offline output exceeds original byte budget')
        receipt = ProviderReceipt(proposal_id=request.proposal_id, attempt_id=request.attempt_id,
            started_at=started, finished_at=now(), elapsed_seconds=time.monotonic()-tick, exit_code=0,
            fixture=True, invocation_started=True, process_stopped=True,
            response_sha256=hashlib.sha256(raw).hexdigest(), stdout_bytes=len(raw))
        return ProviderAttemptResult(raw, None, receipt)
