"""Read-only authentication of an exact original Loop Native attempt.

The factory reuses the production transport verifier and original permanent
session ledger. It does not initialize a Store, migrate a database, dispatch a
model, adopt a material, or turn transport proof into a scientific judgment.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

from .research_quality_v2_binding import (
    QualityBindingError, canonical, digest, read_bounded_json,
    _original_connection, _original_bytes, _sha, _name, _need,
    _decode_record,
)

_FACTORY = object()


class LoopNativeReceiptBinding:
    """A factory-owned immutable proof which rechecks original bytes on use."""
    __slots__ = ('_root', '_pins_json', '_snapshot_json')

    def __init__(self, token, root, pins, snapshot):
        if token is not _FACTORY:
            raise QualityBindingError('Use the original Loop Native binding factory')
        object.__setattr__(self, '_root', str(root))
        object.__setattr__(self, '_pins_json', canonical(pins))
        object.__setattr__(self, '_snapshot_json', canonical(snapshot))

    def __setattr__(self, name, value):
        raise AttributeError('Original receipt bindings are immutable')

    def to_dict(self):
        current = _bind(self._root, **json.loads(self._pins_json))
        _need(canonical(current) == self._snapshot_json,
              'Original Loop Native proof changed after binding')
        return json.loads(self._snapshot_json)

    @property
    def binding_digest(self):
        return digest(self.to_dict())


def _check_parsed_publication(parsed, parsed_sha, proof, path):
    from .server.provider_contract import digest as wire_digest
    _need(wire_digest(parsed) == proof['response_digest'],
          'Parsed publication differs from the original verified response')
    _need(read_bounded_json(path)[1] == parsed_sha,
          'Parsed model publication changed during binding')


def _bind(store_root, *, grant_id, call_id, proposal_id, attempt_id,
          expected_grant_sha256, expected_receipt_sha256,
          expected_context_sha256, expected_raw_sha256,
          expected_profile_digest):
    from .server.provider_loop_native_authority import (
        LoopNativeResearchAuthority, _current_profile, verify_loop_native_transport,
    )
    from .server.provider_host_authority import _decode
    from .server.proposal_requests import ProposalRequests
    from .server.provider_schema import ProviderConfig
    from .server.research_sessions import ResearchSessions
    from .storage import digest as storage_digest

    pins = (expected_grant_sha256, expected_receipt_sha256,
            expected_context_sha256, expected_raw_sha256, expected_profile_digest)
    _need(all(p is not None for p in pins), 'EXTERNAL_PIN_REQUIRED')
    for pin in pins:
        _sha(pin)
    for identity in (grant_id, call_id, proposal_id, attempt_id):
        _name(identity)
    root, connection = _original_connection(store_root)
    store = SimpleNamespace(root=root, db_path=root / 'workbench.sqlite3')
    try:
        authority = LoopNativeResearchAuthority(store, None)
        grant = authority._grant(connection, grant_id)
        _need(storage_digest(grant) == expected_grant_sha256,
              'External original grant pin differs')
        _need(grant['request']['authorization_kind'] == 'bounded_qualification' and
              grant['config']['mode'] == 'codex_cli',
              'Fixture or copied grants are not Native model proof')
        row = connection.execute(
            'SELECT * FROM provider_loop_native_slots WHERE grant_id=? AND call_id=?',
            (grant_id, call_id)).fetchone()
        slot = _decode(row)
        _need(slot['proposal_id'] == proposal_id and slot['attempt_id'] == attempt_id
              and slot['reservation_token'] is not None and slot['dispatch_intent'] is not None,
              'Original slot has no exact charged and dispatched attempt')
        requests = ProposalRequests(store, configs={})
        proposal = requests._load(connection, proposal_id)
        attempts = [a for a in proposal['attempts'] if a['id'] == attempt_id]
        _need(len(attempts) == 1 and proposal['current_attempt_id'] == attempt_id,
              'Only the exact original current attempt may be bound')
        attempt = attempts[0]
        request = proposal['request']
        _need(attempt['status'] not in ('running', 'queued', 'interrupted') and
              proposal['status'] not in ('running', 'validating', 'cancelling'),
              'Unknown or active Native attempt cannot be authenticated')
        _need(slot['request_digest'] == storage_digest(request) and
              slot['context_digest'] == proposal['context_digest'] and
              slot['prepared_digest'] == attempt['prepared_digest'] and
              slot['fencing_token'] == attempt['fencing_token'] and
              slot['spec']['operation'] == request['operation'] and
              slot['spec']['source_kind'] == request['source']['kind'] and
              slot['spec']['candidate_limit'] == request['candidate_limit'] and
              slot['spec']['instruction_digest'] == storage_digest(request['instruction']),
              'Original request differs from its frozen program call')
        session = ResearchSessions(store)._load(connection, request['session_id'])
        reservation = ResearchSessions._reservation(session, slot['reservation_token'])
        _need(slot['session_id'] == request['session_id'] and
              reservation['action_digest'] == slot['request_digest'] and
              reservation['effect_key'] == slot['effect_key'] == request['idempotency_key'] and
              reservation['source_digest'] == request['expected_source_digest'] and
              reservation['charges']['provider_dispatches'] == 1 and
              (reservation['children'] or {}).get('proposal_id') == proposal_id,
              'Original program lost its permanent session charge')
        binding_row = connection.execute(
            'SELECT * FROM provider_loop_native_sessions WHERE session_id=?',
            (request['session_id'],)).fetchone()
        session_binding = _decode(binding_row)
        _need(session_binding['grant_id'] == grant_id and
              session_binding['root_source_digest'] == session['request']['source']['source_digest'],
              'Original session root or program ownership differs')
        config = ProviderConfig.model_validate(proposal['config'])
        _need(config.model_dump() == grant['config'] and
              _current_profile(config) == grant['profile'] and
              grant['profile']['native_identity']['profile_digest'] == expected_profile_digest,
              'Current Native profile/source differs from the original grant')
        folder = root / 'proposals' / proposal_id
        attempt_folder = folder / 'attempts' / attempt_id
        context, context_sha = read_bounded_json(folder / 'context.json')
        receipt, receipt_sha = read_bounded_json(attempt_folder / 'receipt.json')
        raw, raw_sha = _original_bytes(attempt_folder / 'response.raw.json',
                                      request['budget']['max_output_bytes'])
        parsed, parsed_sha = read_bounded_json(attempt_folder / 'parsed.json')
        _need(context_sha == expected_context_sha256 and receipt_sha == expected_receipt_sha256
              and raw_sha == expected_raw_sha256 and context == proposal['context']
              and receipt == attempt['receipt'], 'External context/receipt/raw pins differ')
        origin_row = connection.execute(
            'SELECT payload,digest FROM provider_attempt_origins WHERE attempt_id=?',
            (attempt_id,)).fetchone()
        _need(origin_row is not None, 'Original attempt charge provenance is missing')
        origin = _decode_record(origin_row['payload'])
        charged = [e for e in proposal['events'] if e.get('attempt_id') == attempt_id
                   and e['kind'] == 'attempt_charged']
        _need(len(charged) == 1 and storage_digest(origin) == origin_row['digest']
              == charged[0]['origin_digest'] and
              origin['attempt_charged_digest'] == charged[0]['attempt_digest'] and
              origin['fencing_token'] == attempt['fencing_token'],
              'Original transport lost its event-bound charge origin')
        permit = authority._summary(grant, slot)
        proof = verify_loop_native_transport(store, proposal, attempt, permit,
                                             connection=connection)
        _need(proof['profile_digest'] == expected_profile_digest and
              proof['response_sha256'] == raw_sha,
              'Strict Native transport proof differs from external pins')
        _check_parsed_publication(parsed, parsed_sha, proof, attempt_folder / 'parsed.json')
        # Re-read bounded publications before leaving the read-only snapshot.
        _need(read_bounded_json(folder / 'context.json')[1] == context_sha and
              read_bounded_json(attempt_folder / 'receipt.json')[1] == receipt_sha and
              read_bounded_json(attempt_folder / 'parsed.json')[1] == parsed_sha and
              _original_bytes(attempt_folder / 'response.raw.json',
                              request['budget']['max_output_bytes'])[1] == raw_sha,
              'Original model publications changed during binding')
        return {'schema_version': 1, 'adapter_kind': 'original_loop_native_v3',
            'authority_id': grant['authority_id'], 'program_id': grant['request']['program_id'],
            'plan_digest': grant['request']['plan_digest'], 'grant_id': grant_id,
            'grant_sha256': expected_grant_sha256, 'call_id': call_id,
            'slot_sha256': storage_digest(slot), 'session_id': request['session_id'],
            'reservation_token': slot['reservation_token'], 'reservation': deepcopy(reservation),
            'proposal_id': proposal_id, 'attempt_id': attempt_id,
            'operation': request['operation'], 'source_kind': request['source']['kind'],
            'source_digest': request['expected_source_digest'],
            'request_digest': storage_digest(request), 'request': deepcopy(request),
            'context_digest': proposal['context_digest'], 'context_sha256': context_sha,
            'context': deepcopy(context), 'receipt_sha256': receipt_sha, 'receipt': receipt,
            'raw_sha256': raw_sha, 'parsed_sha256': parsed_sha, 'parsed_output': parsed,
            'profile_digest': expected_profile_digest, 'prepared_digest': attempt['prepared_digest'],
            'permit': permit, 'transport_proof': proof,
            'domain_status': proposal['status'], 'validation': deepcopy(attempt['validation']),
            'outcome': parsed['outcome'], 'model_invocation_authenticated': True,
            'native_current_source_qualified': True, 'semantic_fidelity': 'unverified',
            'quality_judgment': None, 'scientific_score': None,
            'denominator_disposition': 'retain_original_attempt_including_domain_rejection_or_abstention',
            'usage': deepcopy(receipt['usage']), 'wall_seconds': receipt['elapsed_seconds'],
            'binding_scope': 'original_program_charge_and_native_transport_current_source',
            'database_mode': 'ro', 'model_calls_during_binding': 0, 'final_numeric_reads': 0}
    except (KeyError, TypeError, OSError, ValueError) as exc:
        if isinstance(exc, QualityBindingError):
            raise
        raise QualityBindingError('Original Loop Native binding failed: ' +
                                  getattr(exc, 'code', type(exc).__name__)) from exc
    finally:
        connection.close()


def bind_original_loop_native_attempt(store_root, *, grant_id, call_id, proposal_id,
        attempt_id, expected_grant_sha256, expected_receipt_sha256,
        expected_context_sha256, expected_raw_sha256, expected_profile_digest):
    """Authenticate exactly pinned original evidence, never a caller JSON claim.

    grant_sha256 is the SHA of the original storage-JSON grant payload (including
    its canonical indentation/newline), matching the permanent DB digest.
    """
    pins = dict(grant_id=grant_id, call_id=call_id, proposal_id=proposal_id,
        attempt_id=attempt_id, expected_grant_sha256=expected_grant_sha256,
        expected_receipt_sha256=expected_receipt_sha256,
        expected_context_sha256=expected_context_sha256,
        expected_raw_sha256=expected_raw_sha256, expected_profile_digest=expected_profile_digest)
    snapshot = _bind(store_root, **pins)
    return LoopNativeReceiptBinding(_FACTORY, Path(store_root).absolute(), pins, snapshot)
