"""Authoritative accepted candidate packets for bounded experiment handoff.

Only host-owned proposal/session artifacts can supply an experiment Task. The
returned private plan contains exact lineage and normalized tool output; it is
neither an execution permit nor a model-visible context.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib

from ..storage import digest
from .db import connect
from .paper_materials import PaperMaterials
from .provider_contract import digest as wire_digest
from .provider_research_contract import parse_model_output
from .provider_research_context import (build_research_context, resolve_research_source,
                                       validate_material_candidate_task, validate_research_output)
from .provider_research_schema import ResearchProposalCreate
from .provider_schema import ProviderReceipt
from .service import ServiceError


class ExperimentSourceError(ServiceError):
    def __init__(self, message, *, code='EXPERIMENT_SOURCE_INVALID', status=409):
        super().__init__(message, status)
        self.code, self.retryable = code, False


def load_accepted_candidate(store, connection, proposal_id, accepted_response_digest):
    """Read the exact accepted bytes and their already-recorded parent relation.

    No current-source check occurs here: this reader can verify the original
    accepted packet after an owned bootstrap advances the research source. New
    effects must additionally use prepare_experiment_source and a live permit.
    """
    from .proposal_requests import ProposalRequests
    from .research_sessions import ResearchSessions

    proposals = ProposalRequests(store, configs={})
    proposal = proposals._load(connection, proposal_id)
    request = ResearchProposalCreate.model_validate(proposal['request'])
    if (request.operation not in {'generate_candidates', 'repair_candidate'} or proposal['status'] != 'ready'
            or not proposal['accepted_attempt_id'] or not accepted_response_digest
            or proposal['accepted_response_digest'] != accepted_response_digest):
        raise ExperimentSourceError('Experiment requires an exact ready generation or repair response',
                                    code='ACCEPTED_CANDIDATE_REQUIRED')
    attempt = next((item for item in proposal['attempts']
                    if item['id'] == proposal['accepted_attempt_id']), None)
    if (attempt is None or attempt['status'] != 'ready'
            or attempt['response_digest'] != accepted_response_digest
            or (attempt.get('validation') or {}).get('valid') is not True
            or (attempt.get('receipt') or {}).get('process_stopped') is not True):
        raise ExperimentSourceError('Candidate attempt is not an accepted, validated, stopped response',
                                    code='ACCEPTED_CANDIDATE_REQUIRED')
    sessions = ResearchSessions(store)
    loaded = sessions._load(connection, request.session_id)
    action = 'repair' if request.operation == 'repair_candidate' else 'generate'
    parents = [item for item in loaded['reservations'] if item['action'] == action
               and (item['children'] or {}).get('proposal_id') == proposal_id]
    if len(parents) != 1:
        raise ExperimentSourceError('Candidate proposal lacks its actual session child relation',
                                    code='CANDIDATE_NOT_OWNED')
    parent = parents[0]
    if (parent['effect_key'] != request.idempotency_key
            or parent['action_digest'] != digest(request.model_dump())
            or parent['source_digest'] != request.expected_source_digest):
        raise ExperimentSourceError('Candidate proposal differs from its operation reservation',
                                    code='CANDIDATE_NOT_OWNED')
    sessions._children(connection, loaded, parent, parent['children'])
    context = proposal['context']
    if (wire_digest(context) != proposal['context_digest']
            or context['source_digest'] != request.expected_source_digest):
        raise ExperimentSourceError('Accepted candidate context differs from the frozen request',
                                    code='CANDIDATE_SOURCE_MISMATCH')
    folder = proposals._folder(proposal_id)
    raw = proposals._read_path(folder / 'attempts' / attempt['id'] / 'response.raw.json',
                               request.budget.max_output_bytes)
    receipt = ProviderReceipt.model_validate(attempt['receipt'])
    if (receipt.proposal_id != proposal_id or receipt.attempt_id != attempt['id']
            or receipt.response_sha256 != hashlib.sha256(raw).hexdigest()):
        raise ExperimentSourceError('Accepted candidate receipt differs from its original response bytes',
                                    code='CANDIDATE_RECEIPT_MISMATCH')
    output = parse_model_output(raw, proposal_id=proposal_id, attempt_id=attempt['id'],
        context_digest=proposal['context_digest'], operation=request.operation,
        candidate_limit=request.candidate_limit, max_bytes=request.budget.max_output_bytes).model_dump()
    accepted = proposals._json_path(folder / 'accepted' / 'response.json')
    if (output != accepted or output['outcome'] != 'draft'
            or wire_digest(output) != accepted_response_digest):
        raise ExperimentSourceError('Candidate output is not the recorded accepted draft',
                                    code='CANDIDATE_RESPONSE_MISMATCH')
    if request.operation == 'repair_candidate':
        from .provider_correction_native import is_provider_correction, verify_saved_correction
        if is_provider_correction(request):
            verify_saved_correction(store, connection, request, attempt, output['draft'])
        else:
            from .provider_research_targets import resolve_target
            from .provider_repair_semantics import validate_repair_output
            target = resolve_target(store, request, connection=connection)
            diagnostics = validate_repair_output(target['parent_draft'], output['draft'],
                                                 candidate_id=request.target.candidate_id)
            if diagnostics:
                raise ExperimentSourceError('Accepted repair no longer matches its exact parent and allowed edits',
                                            code='REPAIR_TARGET_MISMATCH')
    return {'proposal': proposal, 'request': request.model_dump(), 'output': output,
            'parent_reservation': deepcopy(parent)}


def prepare_experiment_source(store, proposal_id, accepted_response_digest, *, connection=None):
    """Produce a complete, current and normalized Task without any writes.

    The caller freezes this packet in its experiment intent. Before each later
    effect its transaction gate must verify the relevant original/owned source
    and parent budget again; preparation by itself grants no write authority.
    """
    if connection is None:
        with closing(connect(store.db_path)) as opened:
            opened.execute('BEGIN')
            return prepare_experiment_source(store, proposal_id, accepted_response_digest, connection=opened)
    packet = load_accepted_candidate(store, connection, proposal_id, accepted_response_digest)
    proposal, output = packet['proposal'], packet['output']
    request = ResearchProposalCreate.model_validate(packet['request'])
    context = proposal['context']
    from .provider_contract_registry import request_handler
    from .provider_correction_native import is_provider_correction
    versioned_correction = is_provider_correction(request)
    current_context = request_handler(request).build_context(store, request)
    if current_context != context:
        raise ExperimentSourceError('Candidate source changed before experiment preparation', code='SOURCE_CHANGED')
    # New correction has already consumed its one policy pass. Full source/Task
    # validation follows below; rereading the accepted proof never reruns rules.
    checked = (packet['proposal']['attempts'][-1]['validation'] if versioned_correction else
               validate_research_output(store, request, context, output))
    if checked['valid'] is not True:
        raise ExperimentSourceError('Accepted candidate no longer passes its original domain checks',
                                    code='CANDIDATE_VALIDATION_FAILED')
    source = resolve_research_source(store, request.source.model_dump(), connection=connection)
    if digest(source) != request.expected_source_digest:
        raise ExperimentSourceError('Candidate source identity changed', code='SOURCE_CHANGED')
    draft = output['draft']
    material = None
    if request.source.kind == 'accepted_material':
        state, error, validation = validate_material_candidate_task(store, request, context, draft)
        version = PaperMaterials(store).get_version(request.source.material_id, request.source.version_id)
        title, paper_id, dataset_id = request.source.title, version['source']['paper_id'], request.source.dataset_id
        destination = request.source.destination.model_dump()
        operation = 'research.create' if destination['kind'] == 'new_research' else 'revision.create'
        material = {'material_id': request.source.material_id, 'version_id': request.source.version_id,
                    'material_digest': version['digest'], 'draft_digest': version['draft_digest'],
                    'selection_digest': version['selection']['selection_digest'], 'source': deepcopy(version['source'])}
    elif request.source.kind == 'research_revision':
        from .research_draft_validation import validate_research_draft
        state, error, validation = validate_research_draft(store, source=source, draft=draft)
        title, paper_id, dataset_id = (source['research'][key] for key in ('title', 'paper_id', 'dataset_id'))
        destination = {'kind': 'existing_research', 'research_id': request.source.research_id,
                       'base_revision_id': request.source.base_revision_id}
        operation = None
    else:
        raise ExperimentSourceError('Paper extraction cannot directly authorize an experiment',
                                    code='CANDIDATE_SOURCE_UNSUPPORTED')
    if state != 'validated' or validation.get('task') is None:
        raise ExperimentSourceError('Candidate Task cannot be used for an experiment',
                                    code=(error or {}).get('code', 'CANDIDATE_VALIDATION_FAILED'))
    task = validation['task']
    if versioned_correction and digest(task) != checked['correction_proof']['task_digest']:
        raise ExperimentSourceError('Validated Task differs from the original correction proof',
                                    code='CORRECTION_TASK_MISMATCH')
    # A new experiment freezes all checks, including canonical alias changes.
    # It never replaces the original accepted response or labels this as review.
    if (resolve_research_source(store, request.source.model_dump(), connection=connection) != source
            or load_accepted_candidate(store, connection, proposal_id, accepted_response_digest) != packet):
        raise ExperimentSourceError('Candidate or source changed during experiment preparation', code='SOURCE_CHANGED')
    plan = {'schema_version': 1, 'session_id': request.session_id, 'proposal_id': proposal_id,
            'attempt_id': proposal['accepted_attempt_id'], 'accepted_response_digest': accepted_response_digest,
            'request_digest': digest(request.model_dump()), 'context_digest': proposal['context_digest'],
            'source_digest': request.expected_source_digest, 'source_kind': request.source.kind,
            'source_request': request.source.model_dump(), 'source_snapshot': source,
            'destination': destination, 'bootstrap_operation': operation,
            'title': title, 'paper_id': paper_id, 'dataset_id': dataset_id,
            'task': task, 'task_digest': digest(task), 'submitted_draft_digest': digest(draft),
            'candidate_checks': validation['candidate_checks'], 'material': material}
    return {**plan, 'plan_digest': digest(plan)}



def load_corrected_candidate(store, connection, candidate_source):
    """Verify an original host correction for history, without another rule pass."""
    from .research_loop_correction import prepare_corrected_experiment_source as prepare
    from .research_session_experiments_schema import CorrectedCandidateSource
    source = CorrectedCandidateSource.model_validate(candidate_source).model_dump()
    return prepare(store, connection, candidate_source=source, require_current=False)


def prepare_corrected_experiment_source(store, candidate_source, *, connection=None):
    """Use the DB-owned corrected Task; callers cannot supply a replacement Task."""
    if connection is None:
        with closing(connect(store.db_path)) as opened:
            opened.execute('BEGIN')
            return prepare_corrected_experiment_source(store, candidate_source, connection=opened)
    from .research_loop_correction import prepare_corrected_experiment_source as prepare
    from .research_session_experiments_schema import CorrectedCandidateSource
    source = CorrectedCandidateSource.model_validate(candidate_source).model_dump()
    return prepare(store, connection, candidate_source=source, require_current=True)
