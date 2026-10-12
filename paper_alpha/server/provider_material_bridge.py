"""Import material only from an actual accepted v2 proposal and session ledger.

All authority is reconstructed from host-owned artifacts. Callers supply a
proposal identity, never a supposedly accepted packet or a model-written path.
"""
from __future__ import annotations

from contextlib import closing
import hashlib

from ..storage import digest
from .db import connect
from .paper_materials import PaperMaterials
from .paper_segments import MaterialError, validate_material_draft, validate_selection
from .provider_contract import digest as wire_digest
from .provider_contract_registry import request_handler
from .provider_research_schema import ResearchProposalCreate
from .provider_schema import ProviderReceipt


def _accepted_material(store, connection, proposal_id, attempt_id=None):
    from .proposal_requests import ProposalRequests
    from .research_sessions import ResearchSessions

    proposals = ProposalRequests(store, configs={})
    proposal = proposals._load(connection, proposal_id)
    request = ResearchProposalCreate.model_validate(proposal['request'])
    actual_attempt_id = proposal['accepted_attempt_id']
    if (proposal['status'] != 'ready' or request.operation != 'extract_material'
            or request.source.kind != 'paper_selection' or not actual_attempt_id
            or not proposal['accepted_response_digest']
            or attempt_id is not None and attempt_id != actual_attempt_id):
        raise MaterialError('Material import requires the exact ready extraction proposal',
                            code='ACCEPTED_PACKET_UNVERIFIED')
    attempt = next((item for item in proposal['attempts'] if item['id'] == actual_attempt_id), None)
    if (attempt is None or attempt['status'] != 'ready'
            or attempt['response_digest'] != proposal['accepted_response_digest']
            or (attempt.get('validation') or {}).get('valid') is not True
            or (attempt.get('receipt') or {}).get('process_stopped') is not True):
        raise MaterialError('Accepted material attempt is not verified and stopped',
                            code='ACCEPTED_PACKET_UNVERIFIED')
    sessions = ResearchSessions(store)
    loaded = sessions._load(connection, request.session_id)
    owned = [item for item in loaded['reservations']
             if item['action'] == 'extract' and (item['children'] or {}).get('proposal_id') == proposal_id]
    if len(owned) != 1:
        raise MaterialError('Extraction proposal is not an actual child of its research session',
                            code='ACCEPTED_PACKET_UNVERIFIED')
    reservation = owned[0]
    if (reservation['effect_key'] != request.idempotency_key
            or reservation['action_digest'] != digest(request.model_dump())
            or reservation['source_digest'] != request.expected_source_digest):
        raise MaterialError('Extraction child differs from its reserved source or effect',
                            code='ACCEPTED_PACKET_UNVERIFIED')
    sessions._children(connection, loaded, reservation, reservation['children'])
    context = proposal['context']
    selection = validate_selection(context['paper_selection'])
    source_digest = digest({'kind': 'paper_selection', 'paper_id': selection['paper_id'],
                            'selection_digest': selection['selection_digest'], 'selection': selection})
    if (wire_digest(context) != proposal['context_digest']
            or context['source_digest'] != request.expected_source_digest
            or source_digest != context['source_digest']
            or selection['paper_id'] != request.source.paper_id
            or selection['selected_pages'] != request.source.selected_pages):
        raise MaterialError('Accepted material context differs from the frozen selection',
                            code='ACCEPTED_PACKET_UNVERIFIED')
    folder = proposals._folder(proposal_id)
    raw = proposals._read_path(folder / 'attempts' / actual_attempt_id / 'response.raw.json',
                               request.budget.max_output_bytes)
    receipt = ProviderReceipt.model_validate(attempt['receipt'])
    if (receipt.proposal_id != proposal_id or receipt.attempt_id != actual_attempt_id
            or receipt.response_sha256 != hashlib.sha256(raw).hexdigest()):
        raise MaterialError('Accepted material receipt differs from the exact response bytes',
                            code='ACCEPTED_PACKET_UNVERIFIED')
    parsed = request_handler(request).parse_output(raw, request=request, context=context, proposal_id=proposal_id, attempt_id=actual_attempt_id,
                context_digest=proposal['context_digest'],
                candidate_limit=request.candidate_limit, max_bytes=request.budget.max_output_bytes).model_dump()
    accepted = proposals._json_path(folder / 'accepted' / 'response.json')
    if (parsed != accepted or parsed['outcome'] != 'material'
            or wire_digest(parsed) != proposal['accepted_response_digest']):
        raise MaterialError('Accepted material bytes do not belong to the recorded attempt',
                            code='ACCEPTED_PACKET_UNVERIFIED')
    checked = validate_material_draft(selection, parsed['material'])
    packet = {'contract_id': 'research_provider_v2', 'operation': 'extract_material',
              'proposal_id': proposal_id, 'attempt_id': actual_attempt_id,
              'accepted_response_digest': proposal['accepted_response_digest'],
              'selection_digest': selection['selection_digest'],
              'material_digest': digest(checked['material'])}
    return packet, selection, checked['material']


def accepted_packet_resolver(store):
    def resolve(connection, *, proposal_id, attempt_id):
        return _accepted_material(store, connection, proposal_id, attempt_id)[0]
    return resolve


def import_accepted_material(store, proposal_id):
    with closing(connect(store.db_path)) as connection:
        connection.execute('BEGIN')
        packet, selection, draft = _accepted_material(store, connection, proposal_id)
    # PaperMaterials re-resolves the packet under its commit transaction, as
    # well as verifying current PDF bytes before a new material is created.
    return PaperMaterials(store, accepted_packet_resolver=accepted_packet_resolver(store)).create_from_proposal(
        proposal_id=packet['proposal_id'], attempt_id=packet['attempt_id'],
        accepted_response_digest=packet['accepted_response_digest'], selection=selection, draft=draft)
