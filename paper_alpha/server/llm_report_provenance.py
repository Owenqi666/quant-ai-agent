"""Freeze host-owned model-stage receipts in the report's existing read snapshot."""
from copy import deepcopy

from ..storage import digest
from .evidence_lineage import EvidenceLineage
from .paper_materials import PaperMaterials
from .proposal_requests import ProposalRequests
from .provider_experiment_source import load_accepted_candidate
from .research_assessments import assessment_summary
from .service import ServiceError


def human_review_status(reviews):
    """Declared human judgments remain separate from automated acceptance."""
    human = [review for review in reviews if review['source'] == 'human']
    judgments = [{'review_id': review['id'], **assessment_summary(review.get('assessment'), 'human')}
                 for review in human]
    complete = any(item['semantic_status'] in {'passed', 'failed'} for item in judgments)
    return {'status': 'human_judgment_recorded' if complete else 'pending_human',
            'human_review_ids': [review['id'] for review in human], 'judgments': judgments,
            'identity_scope': 'declared_source_not_authenticated'}


def _proposal_stage(proposal):
    accepted = proposal['accepted_attempt_id']
    attempt = next(item for item in proposal['attempts'] if item['id'] == accepted)
    receipt = attempt['receipt']
    kind = ('fixture' if receipt['fixture'] else
            'native_provider_invocation' if receipt['invocation_started'] else 'stored_response')
    return {'status': 'recorded', 'execution_kind': kind, 'operation': proposal['request']['operation'],
            'proposal_id': proposal['id'], 'attempt_id': accepted,
            'context_digest': proposal['context_digest'],
            'accepted_response_digest': proposal['accepted_response_digest'],
            'receipt_digest': digest(receipt), 'receipt': deepcopy(receipt),
            'source_kind': proposal['request']['source']['kind'],
            'provider_config_id': proposal['request']['provider_config_id'],
            'provider_mode': proposal['config']['mode'],
            'native_transport': proposal['config'].get('native_transport', 'exec'),
            'scope': 'The receipt records an invocation; official transport identity and real-model acceptance are separate evidence.'}


def freeze_stage_provenance(store, connection, source):
    """Reuse accepted-packet and lineage checks; never inspect a provider registry."""
    unknown = {'status': 'unknown', 'execution_kind': 'unknown', 'reason_code': 'UPSTREAM_NOT_RECORDED'}
    value = {'schema_version': 1, 'basis': 'host_owned_saved_receipts',
             'extraction': deepcopy(unknown), 'generation': deepcopy(unknown),
             'adoption': {'status': 'unknown', 'reason_code': 'MATERIAL_LINEAGE_NOT_RECORDED'},
             'calculation': {'status': source['run']['status'], 'execution_kind': 'deterministic_tool',
                             'llm_calls': 0, 'tool_calls': source['summary']['tool_calls'],
                             'elapsed_seconds': source['summary']['elapsed_seconds']},
             'report': {'execution_kind': 'deterministic_snapshot', 'llm_calls': 0},
             'semantic_fidelity': 'unverified'}
    if not source['summary']['source_verified']:
        value['generation']['reason_code'] = 'RUN_SOURCE_UNVERIFIED'
        return value
    reader = EvidenceLineage(store)
    try:
        intent = reader._owner(connection, source['run'])
        if intent is None:
            return value
        plan = intent['plan']
        packet = load_accepted_candidate(store, connection, plan['proposal_id'], plan['accepted_response_digest'])
        # The lineage reader binds the same frozen task to its actual run/job
        # effects, even when no original extraction/material chain was recorded.
        state = source['state']
        records = []
        for candidate in state['candidates']:
            hypothesis = next(item for item in state['task']['hypotheses'] if item['id'] == candidate['hypothesis_id'])
            evidence = [next(item for item in state['task']['evidence'] if item['id'] == identity)
                        for identity in hypothesis['evidence_ids']]
            target = {'run_id': source['run']['id'], 'revision_id': source['run']['revision_id'],
                      'candidate_id': candidate['id'], 'attempt_id': source['run']['attempt_id'],
                      'state_digest': digest(state), 'result_digest': digest(candidate)}
            records.append(reader._verified(connection, source['run'], state, hypothesis, evidence, target, intent))
        value['generation'] = _proposal_stage(packet['proposal'])
        value['generation']['experiment_id'] = intent['id']
        if records and all(item['status'] == 'verified' for item in records):
            proof = records[0]['provenance']
            extraction = ProposalRequests(store, configs={})._load(connection, proof['extraction']['proposal_id'])
            value['extraction'] = _proposal_stage(extraction)
            materials = PaperMaterials(store)
            draft_row = connection.execute('SELECT * FROM paper_materials WHERE id=?',
                                           (proof['material']['id'],)).fetchone()
            version, history = reader._versions(connection, materials, materials._draft(draft_row), plan['material'])
            value['adoption'] = {'status': 'recorded', 'material': proof['material'],
                                 'actor': version.get('actor', 'legacy_unknown'),
                                 'selection': proof['selection'],
                                 'history': [{'version_id': item['id'], 'digest': item['digest'],
                                              'actor': item.get('actor', 'legacy_unknown'),
                                              'changed_paths': [change['path'] for change in item['changes']]}
                                             for item in history],
                                 'candidate_lineage': [{'target': item['target'], 'hypothesis': item['hypothesis'],
                                                        'evidence': item['evidence']} for item in records],
                                 'human_semantic_approval': False}
        return value
    except (ServiceError, ValueError, KeyError, OSError, StopIteration) as exc:
        code = getattr(exc, 'code', 'STAGE_PROVENANCE_UNVERIFIED')
        value['generation'] = {**unknown, 'reason_code': code}
        value['extraction'] = {**unknown, 'reason_code': code}
        return value
