"""Verify exact native v2 attempt evidence; never authorize another request.

These are local consistency checks, not signatures against a host that can
replace code and all records. The evaluation authority owns budget and scope.
"""
from contextlib import closing
import hashlib

from ..storage import digest
from .db import connect
from .provider_contract import strict_json, canonical_json, digest as wire_digest
from .provider_contract_registry import request_handler
from .provider_schema import ProviderConfig, ProviderReceipt
from .provider_supervision_journal import read_metadata, termination_proof
from .service import ServiceError
from .provider_native_transport_identity import native_transport

QUALIFICATION_KIND = 'g4_generate_development_v1'
PRE_DISPATCH_FAILURE_KIND = 'app_server_confirmed_pre_dispatch_failure_v1'
PRE_DISPATCH_FAILURE_PHASE = 'after_version_before_auth_validation_phase'
PRE_DISPATCH_CONTEXT_ERRORS = frozenset('BUSINESS_APP_SERVER_' + name for name in (
    'CONTEXT_UNSAFE', 'CONTEXT_POLLUTED', 'CONTEXT_METADATA_LIMIT',
    'CONTEXT_ARGUMENT_INVALID', 'CONTEXT_PATH_INVALID', 'ARG0_UNSAFE'))


class ResearchQualificationError(ServiceError):
    def __init__(self, message, *, code='G4_NATIVE_EVIDENCE_INVALID'):
        super().__init__(message, 409)
        self.code, self.retryable = code, False


def _require(condition, message):
    if not condition:
        raise ResearchQualificationError(message)


def verify_native_attempt(store, proposal, attempt, result=None):
    return _verify_native_attempt(store, proposal, attempt, result, scope='legacy')


def verify_host_native_attempt(store, proposal, attempt, result=None):
    return _verify_native_attempt(store, proposal, attempt, result, scope='host')


def _verify_native_attempt(store, proposal, attempt, result=None, *, scope):
    """Re-read the native transport and exact original supervisor chain.

``result`` supplies the just-returned parsed response and receipt at acceptance;
it is compared to original bounded files, never treated as its own evidence.
Omitting it verifies a persisted attempt without invoking any executable.
"""
    from .proposal_requests import ProposalRequests
    from .provider_native import _identity, _boundaries_match, _invocation_boundaries_match
    from .codex_cli_provider import _bounded_private_file
    from .provider_research_schema import CONTRACT_ID
    try:
        if native_transport(proposal['config']) == 'app_server':
            return verify_app_server_attempt_transport(store, proposal, attempt, result, scope=scope)
        requests = ProposalRequests(store, configs={})
        request = proposal['request']
        allowed = (request['operation'] == 'generate_candidates') if scope == 'legacy' else (
            (request['operation'], request['source']['kind']) in {('extract_material', 'paper_selection'), ('generate_candidates', 'accepted_material')})
        _require(request.get('schema_version') == 2 and request.get('contract_id') == CONTRACT_ID
                 and allowed, 'Operation is outside this independent qualification scope')
        _require(attempt['id'] == proposal['current_attempt_id'] and attempt['prepared_digest'] is not None,
                 'Qualification requires the exact prepared attempt')
        config = ProviderConfig.model_validate(proposal['config'])
        _require(config.mode == 'codex_cli' and config.model == 'gpt-5.5'
                 and config.reasoning_effort == 'low' and config.expected_cli_version == '0.145.0',
                 'Native configuration differs from the pinned development scope')
        identity, profile = _identity(config, contract_id=CONTRACT_ID)
        _require(profile['static_checks']['passed'], 'Current v2 profile is unavailable')
        folder = requests._folder(proposal['id']) / 'attempts' / attempt['id']
        transport = folder / 'transport'
        native = read_metadata(transport / 'native-evidence.json', 512 * 1024)
        saved_profile = read_metadata(transport / 'profile.json', 512 * 1024)
        intent = read_metadata(transport / 'dispatch-intent.json')
        prepared = read_metadata(folder / 'prepared.json', 512 * 1024)
        _require(digest(prepared) == attempt['prepared_digest'], 'Prepared input anchor differs')
        _require(saved_profile == profile and native['identity'] == identity and intent['identity'] == identity,
                 'Native evidence belongs to another profile or changed source')
        _require(native.get('contract_id') == CONTRACT_ID, 'Native evidence lacks its exact v2 contract')
        _require(native['host_authorized'] is True and native['automatic_provider_retry'] is False
                 and native['native_authentication_confirmed'] is True and native['dispatch_started'] is True,
                 'Native authentication, explicit dispatch or no-retry evidence is incomplete')
        _require(intent['proposal_id'] == proposal['id'] and intent['attempt_id'] == attempt['id']
                 and type(intent['provider_invocations']) is int and intent['provider_invocations'] == 1,
                 'Native intent names another attempt')
        handler = request_handler(request)
        _require(prepared['proposal_id'] == proposal['id'] and prepared['attempt_id'] == attempt['id']
                 and prepared['provider_config'] == config.model_dump()
                 and prepared['context_digest'] == proposal['context_digest']
                 and prepared['output_schema'] == handler.output_schema(proposal['context']), 'Prepared identity or schema differs')
        input_bytes = _bounded_private_file(transport / 'input.txt', request['budget']['max_context_bytes'])
        _require(input_bytes == prepared['input_text'].encode(), 'Native input differs from queued input')
        _require(read_metadata(transport / 'output.schema.json', 512 * 1024) == prepared['output_schema'],
                 'Native schema differs from the prepared schema')
        invocation = read_metadata(transport / 'invocation.json', 512 * 1024)
        _require(wire_digest(invocation) == native['invocation_digest'] == intent['invocation_digest'],
                 'Actual invocation arguments differ from the dispatch and native evidence')
        _require(_invocation_boundaries_match(invocation, identity), 'Actual outer Native boundary differs')
        _require(invocation['identity'] == identity and invocation['input_sha256'] == hashlib.sha256(input_bytes).hexdigest()
                 and invocation['schema_digest'] == wire_digest(prepared['output_schema'])
                 and invocation['automatic_provider_retry'] is False, 'Native invocation identity differs')
        for kind in ('read', 'write'):
            report = read_metadata(transport / (kind + '-boundary') / 'report.json', 512 * 1024)
            _require(_boundaries_match(report, kind, identity, profile)
                     and native['boundary_checks'][kind] == {'process_stopped': True, 'verified': True},
                     'Native tool boundary evidence failed verification')
        saved = read_metadata(transport / 'receipt.json')
        actual = result if result is not None else {'receipt': attempt['receipt'], 'error': attempt['error']}
        receipt = ProviderReceipt.model_validate(actual['receipt']).model_dump()
        _require(actual['error'] is None and saved == {'receipt': receipt, 'error': None},
                 'Returned receipt differs from the original successful native attempt')
        _require(receipt['proposal_id'] == proposal['id'] and receipt['attempt_id'] == attempt['id']
                 and receipt['fixture'] is False and receipt['invocation_started'] is True
                 and receipt['process_stopped'] is True and receipt['exit_code'] == 0
                 and receipt['requested_model'] == config.model and receipt['cli_version'] == config.expected_cli_version,
                 'Native receipt does not establish a completed pinned real attempt')
        raw = _bounded_private_file(transport / 'response.raw.json', request['budget']['max_output_bytes'])
        _require(raw == requests._read_path(folder / 'response.raw.json', request['budget']['max_output_bytes'])
                 and hashlib.sha256(raw).hexdigest() == receipt['response_sha256'], 'Original response bytes differ')
        parsed = handler.parse_output(raw, request=request, context=proposal['context'], proposal_id=proposal['id'], attempt_id=attempt['id'],
            context_digest=proposal['context_digest'], candidate_limit=request['candidate_limit'],
            max_bytes=request['budget']['max_output_bytes']).model_dump()
        expected_output = actual.get('response') if result is not None else read_metadata(folder / 'parsed.json')
        _require(parsed == expected_output, 'Parsed model output differs from its exact native response')
        with closing(connect(store.db_path)) as connection:
            origin_row = connection.execute('SELECT payload,digest FROM provider_attempt_origins WHERE attempt_id=?',
                                            (attempt['id'],)).fetchone()
            workspace = connection.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()[0]
        _require(origin_row is not None, 'Attempt has no original charged provenance')
        origin = strict_json(origin_row['payload'].encode(), max_bytes=65536)
        _require(digest(origin) == origin_row['digest'], 'Attempt origin digest differs')
        proof = termination_proof(store.root, proposal, workspace, origin)
        _require(proof['confirmed'] is True and proof['reason'] == 'BOUND_STOP_EVIDENCE',
                 'Exact live supervisor termination was not established')
        stop = read_metadata(transport / 'real-supervision.json')
        matches = []
        for path in (folder / 'supervision').iterdir():
            if path.is_dir():
                finished = read_metadata(path / 'finished.json')
                actual_stop = finished.get('stop') or {}
                if actual_stop.get('nonce') == stop.get('nonce'):
                    matches.append(actual_stop)
        _require(len(matches) == 1 and all(matches[0].get(key) == value for key, value in stop.items()),
                 'Native termination metadata differs from its exact supervisor')
        _require(stop['invocation_started'] is True and stop['process_stopped'] is True
                 and stop['exit_code'] == 0 and stop['response_saved'] is True
                 and stop['response_sha256'] == receipt['response_sha256'], 'Native final response has no successful stop proof')
        _require(_identity(config, contract_id=CONTRACT_ID)[0] == identity, 'Source changed during evidence verification')
        return {'qualification_kind': QUALIFICATION_KIND if scope == 'legacy' else 'host_material_native_v1', 'contract_id': CONTRACT_ID,
            'operation': request['operation'], 'proposal_id': proposal['id'], 'attempt_id': attempt['id'],
            'context_digest': proposal['context_digest'], 'prepared_digest': attempt['prepared_digest'],
            'profile_digest': profile['profile_digest'], 'config_digest': wire_digest(config.model_dump()),
            'native_evidence_digest': digest(native), 'dispatch_intent_digest': digest(intent),
            'response_sha256': receipt['response_sha256'], 'response_digest': wire_digest(parsed),
            'receipt_digest': digest(receipt), 'supervision_digest': digest(proof),
            'requested_model': config.model, 'reported_model': receipt['reported_model'],
            'semantic_fidelity': 'unverified', 'backend_model_calls': receipt['usage']['model_calls'],
            'supplier_cost': receipt['usage']['cost']}
    except ResearchQualificationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ResearchQualificationError('Native attempt evidence is unavailable, malformed or inconsistent') from exc


def verify_app_server_attempt_transport(store, proposal, attempt, result=None, *,
                                        expected_permit=None, connection=None, scope='legacy'):
    """Verify original App Server bytes and the permanent charged attempt.

    This is a saved consistency verifier, not a dispatch permission. Domain
    rejection remains independently recorded by the original Worker/Loop.
    """
    from contextlib import nullcontext
    from .proposal_requests import ProposalRequests
    from .provider_research_schema import CONTRACT_ID
    from .provider_schema import ProviderUsage
    from .codex_cli_provider import _bounded_private_file
    from .provider_native_transport_identity import current_native_identity, native_profile_digest
    from .provider_app_server_business_identity import (
        BUSINESS_DISPATCH_TRANSPORT_VERSION, business_dispatch_invocation_boundaries_match,
    )
    from .provider_app_server_evidence import load_business_capture_evidence
    from .provider_app_server_capture import capture_version_for_transport
    from .provider_app_server_qualification import verify_app_server_fresh_evidence
    try:
        _require(scope in {'legacy', 'host', 'loop'}, 'Unsupported qualification scope')
        request = proposal['request']
        _require(request.get('schema_version') == 2 and request.get('contract_id') == CONTRACT_ID
                 and request.get('operation') == 'generate_candidates',
                 'App Server qualification supports only the original generation operation')
        if scope == 'host':
            _require(request['source']['kind'] == 'accepted_material', 'App Server host scope differs')
        if scope == 'loop':
            _require(request['source']['kind'] in {'research_revision', 'accepted_material'},
                     'App Server loop source is outside the generation scope')
        config = ProviderConfig.model_validate(proposal['config'])
        _require(native_transport(config) == 'app_server' and config.mode == 'codex_cli'
                 and config.model == 'gpt-5.5' and config.reasoning_effort == 'low'
                 and config.expected_cli_version == '0.145.0', 'App Server configuration differs')
        _require(attempt['id'] == proposal['current_attempt_id'] and attempt['prepared_digest'] is not None,
                 'App Server proof requires the original prepared attempt')
        requests = ProposalRequests(store, configs={})
        identity, profile = current_native_identity(config, contract_id=CONTRACT_ID)
        _require(profile['static_checks']['passed'] is True, 'Current App Server profile is unavailable')
        profile_digest = native_profile_digest(identity)
        folder = requests._folder(proposal['id']) / 'attempts' / attempt['id']
        transport = folder / 'transport'
        native = read_metadata(transport / 'native-evidence.json', 768 * 1024)
        intent = read_metadata(transport / 'dispatch-intent.json', 768 * 1024)
        invocation = read_metadata(transport / 'invocation.json', 1024 * 1024)
        prepared = read_metadata(folder / 'prepared.json', 512 * 1024)
        handler = request_handler(request)
        body = handler.parse_request(request)
        expected_prepared = {'proposal_id': proposal['id'], 'attempt_id': attempt['id'],
            'context_digest': proposal['context_digest'],
            'input_text': handler.build_prompt(body, proposal['context'], proposal['id'], attempt['id']),
            'output_schema': handler.output_schema(proposal['context']), 'provider_config': proposal['config'],
            'budget': request['budget']}
        _require(prepared == expected_prepared and digest(prepared) == attempt['prepared_digest'],
                 'Prepared generation differs from the original context/attempt')
        _require(read_metadata(transport / 'profile.json', 768 * 1024) == profile
                 and native.get('identity') == intent.get('identity') == identity
                 and native.get('transport_version') == BUSINESS_DISPATCH_TRANSPORT_VERSION
                 and intent.get('transport_version') == BUSINESS_DISPATCH_TRANSPORT_VERSION,
                 'App Server evidence belongs to another current identity or transport')
        _require(native.get('contract_id') == CONTRACT_ID and native.get('host_authorized') is True
                 and native.get('automatic_provider_retry') is False
                 and native.get('native_authentication_confirmed') is True
                 and native.get('dispatch_started') is True,
                 'Original no-retry/authenticated dispatch evidence is incomplete')
        _require(intent.get('proposal_id') == proposal['id'] and intent.get('attempt_id') == attempt['id']
                 and type(intent.get('provider_invocations')) is int and intent['provider_invocations'] == 1,
                 'App Server dispatch intent belongs to another call')
        if expected_permit is not None:
            _require(native.get('research_authorization_dispatch') == expected_permit
                     and intent.get('research_authorization') == expected_permit
                     and native.get('research_authorization_entry') == {**expected_permit, 'dispatch_intent_digest': None},
                     'App Server dispatch does not match its original permanent permit')
        _require(business_dispatch_invocation_boundaries_match(invocation, identity),
                 'Current App Server invocation boundary/plan differs')
        _require(wire_digest(invocation) == native.get('invocation_digest') == intent.get('invocation_digest'),
                 'App Server invocation differs from the original intent')
        inputs = _bounded_private_file(transport / 'input.txt', body.budget.max_context_bytes)
        _require(inputs == prepared['input_text'].encode('utf-8')
                 and read_metadata(transport / 'output.schema.json', 512 * 1024) == prepared['output_schema'],
                 'Actual input or schema differs from the original prepared bytes')
        fresh = verify_app_server_fresh_evidence(native, config=config, expected_current_identity=identity)
        _require(fresh.get('read_verified') is True and fresh.get('write_verified') is True,
                 'Current controlled App Server boundary evidence is unverified')
        _require(native.get('boundary_checks') == {'read': {'process_stopped': True, 'verified': True},
                                                 'write': {'process_stopped': True, 'verified': True}},
                 'App Server boundary receipt differs from its raw evidence')
        actual = result if result is not None else {'receipt': attempt['receipt'], 'error': attempt['error']}
        receipt = ProviderReceipt.model_validate(actual['receipt']).model_dump()
        saved_receipt = read_metadata(transport / 'receipt.json')
        # The loop observer receives a domain rejection after transport completed.
        # Its independent native receipt still records a transport error of None.
        _require(saved_receipt == {'receipt': receipt, 'error': None}
                 and (result is None or actual.get('error') is None),
                 'Returned receipt differs from the original successful transport')
        if scope != 'loop':
            _require(actual.get('error') is None, 'Qualification is not a successful domain attempt')
        _require(receipt['proposal_id'] == proposal['id'] and receipt['attempt_id'] == attempt['id']
                 and receipt['fixture'] is False and receipt['invocation_started'] is True
                 and receipt['process_stopped'] is True and receipt['exit_code'] == 0
                 and receipt['requested_model'] == config.model and receipt['cli_version'] == config.expected_cli_version,
                 'Original receipt lacks a completed pinned Native generation')
        with (nullcontext(connection) if connection is not None else closing(connect(store.db_path))) as opened:
            origin_row = opened.execute('SELECT payload,digest FROM provider_attempt_origins WHERE attempt_id=?',
                                        (attempt['id'],)).fetchone()
            workspace_row = opened.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()
            _require(origin_row is not None and workspace_row is not None, 'Original charge/workspace is missing')
            origin = strict_json(origin_row['payload'].encode(), max_bytes=65536)
            _require(digest(origin) == origin_row['digest'], 'Original charged origin digest differs')
            proof = termination_proof(store.root, proposal, workspace_row[0], origin)
        _require(proof.get('confirmed') is True and proof.get('reason') == 'BOUND_STOP_EVIDENCE',
                 'Original live supervisor lacks its bound termination proof')
        binding = read_metadata(folder / 'supervision' / 'binding.json')
        anchors = {event['attempt_digest'] for event in proposal['events']
                   if event.get('attempt_id') == attempt['id'] and event['kind'] == 'invocation_prepared'}
        binding_keys = {'schema_version', 'workspace_identity', 'proposal_id', 'attempt_id', 'fencing_token',
                        'prepared_digest', 'config_digest', 'attempt_started_digest', 'host_boot'}
        _require(set(binding) == binding_keys and type(binding['schema_version']) is int and binding['schema_version'] == 1
                 and binding['workspace_identity'] == origin['workspace_identity']
                 and binding['host_boot'] == origin['host_boot']
                 and binding['proposal_id'] == proposal['id'] and binding['attempt_id'] == attempt['id']
                 and binding['fencing_token'] == attempt['fencing_token']
                 and binding['prepared_digest'] == attempt['prepared_digest']
                 and binding['config_digest'] == digest(proposal['config'])
                 and binding['attempt_started_digest'] in anchors,
                 'Saved capture binding differs from the original prepared/charged anchors')
        loaded = load_business_capture_evidence(transport / 'app-server-capture', expected_identity=invocation['identity'],
                                               expected_attempt_binding=binding)
        capture = loaded['capture']
        _require(capture.get('capture_version') == capture_version_for_transport(BUSINESS_DISPATCH_TRANSPORT_VERSION)
                 and capture.get('valid') is True and capture.get('error') is None
                 and 'primary_error' in capture and capture['primary_error'] is None
                 and 'secondary_capture_error' in capture and capture['secondary_capture_error'] is None
                 and native.get('capture') == capture,
                 'Native summary differs from the replayed original RPC capture')
        _require(loaded['invocation'] == invocation and loaded['plan']['prompt'] == prepared['input_text']
                 and loaded['plan']['output_schema'] == prepared['output_schema'],
                 'Persisted RPC plan differs from the exact prepared input')
        raw = _bounded_private_file(transport / 'response.raw.json', body.budget.max_output_bytes)
        _require(raw == loaded['response'] == requests._read_path(folder / 'response.raw.json', body.budget.max_output_bytes)
                 and hashlib.sha256(raw).hexdigest() == receipt['response_sha256'],
                 'Original response bytes differ from the complete RPC/Worker evidence')
        if attempt.get('raw_sha256') is not None:
            _require(attempt['raw_sha256'] == receipt['response_sha256'], 'Queue raw response hash differs')
        parsed = handler.parse_output(raw, request=body, context=proposal['context'], proposal_id=proposal['id'],
            attempt_id=attempt['id'], context_digest=proposal['context_digest'], candidate_limit=body.candidate_limit,
            max_bytes=body.budget.max_output_bytes).model_dump()
        expected_output = actual.get('response') if result is not None else read_metadata(folder / 'parsed.json')
        _require(parsed == expected_output, 'Parsed generation differs from the original RPC final JSON')
        _require(receipt['usage'] == ProviderUsage.model_validate(capture['usage']).model_dump()
                 and receipt['reported_model'] == capture.get('reported_model'),
                 'Provider receipt usage/model differs from the replayed reported observations')
        stop = read_metadata(transport / 'real-supervision.json')
        actual_stop = loaded['stop']
        _require(type(stop.get('protocol_version')) is int and stop['protocol_version'] == 2
                 and stop.get('transport') == 'app_server' and stop.get('rpc_completed') is True
                 and stop == actual_stop
                 and stop.get('response_sha256') == receipt['response_sha256']
                 and stop.get('rpc_result_sha256') == loaded['envelope']['raw_manifest']['rpc-result.json']['sha256'],
                 'App Server stop/RPC differs from the original complete supervisor capture')
        matches = []
        for path in (folder / 'supervision').iterdir():
            if path.is_dir() and not path.is_symlink():
                final = read_metadata(path / 'finished.json')
                original_stop = final.get('stop') or {}
                if original_stop.get('nonce') == actual_stop.get('nonce'):
                    matches.append((path, final))
        _require(len(matches) == 1 and matches[0][1] == loaded['journal']['finished'],
                 'Copied RPC capture differs from the original supervisor journal')
        original_stage = matches[0][0]
        _require(read_metadata(original_stage / 'prepared.json') == loaded['journal']['prepared']
                 and read_metadata(original_stage / 'started.json') == loaded['journal']['started']
                 and binding == loaded['journal']['binding'],
                 'Copied RPC prepared/PID/binding differs from the original persistent journal')
        _require(current_native_identity(config, contract_id=CONTRACT_ID)[0] == identity,
                 'App Server source changed during persisted evidence verification')
        return {'qualification_kind': 'app_server_generation_transport_v1', 'contract_id': CONTRACT_ID,
            'transport_version': BUSINESS_DISPATCH_TRANSPORT_VERSION, 'operation': body.operation,
            'proposal_id': proposal['id'], 'attempt_id': attempt['id'],
            'context_digest': proposal['context_digest'], 'prepared_digest': attempt['prepared_digest'],
            'profile_digest': profile_digest, 'config_digest': wire_digest(config.model_dump()),
            'native_evidence_digest': digest(native), 'dispatch_intent_digest': digest(intent),
            'response_sha256': receipt['response_sha256'], 'response_digest': wire_digest(parsed),
            'receipt_digest': digest(receipt), 'supervision_digest': digest(proof),
            'rpc_result_sha256': stop['rpc_result_sha256'], 'fresh_report_digest': fresh['report_digest'],
            'requested_model': config.model, 'reported_model': receipt['reported_model'],
            'semantic_fidelity': 'unverified', 'backend_model_calls': receipt['usage']['model_calls'],
            'supplier_cost': receipt['usage']['cost'], 'authority_id': 'research_loop_native_v1' if scope == 'loop' else None}
    except ResearchQualificationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ResearchQualificationError('App Server attempt evidence is unavailable, malformed or inconsistent') from exc


def _pre_dispatch_journal(journal_root, binding, *, fresh_root, version_stop):
    """Re-read twelve original persisted stops; never follow saved PID paths."""
    from pathlib import Path
    from .provider_supervision_journal import validate_stop
    from .provider_read_probe import READ_BOUNDARY_CASES
    from .provider_write_probe import CASES
    from .provider_app_server_write_intervention_probe import PHASES
    root = Path(journal_root)
    _require(read_metadata(root / 'binding.json') == binding, 'Original failure binding differs')
    stages = sorted(path for path in root.iterdir() if path.name != 'binding.json')
    _require(len(stages) == 12 and all(path.is_dir() and not path.is_symlink() for path in stages),
             'Failure requires exactly eleven fresh stages and one version stage')
    fresh_paths = [Path(fresh_root) / 'legacy' / name / 'supervision' / 'stop.json'
                   for name in READ_BOUNDARY_CASES + CASES]
    fresh_paths += [Path(fresh_root) / 'write' / name / 'supervision' / 'stop.json' for name in PHASES]
    _require(len(fresh_paths) == 11, 'Fresh stage inventory differs')
    descriptors = []
    for sequence, path in enumerate(stages, 1):
        entry = read_metadata(path / 'prepared.json')
        started = read_metadata(path / 'started.json')
        finished = read_metadata(path / 'finished.json')
        fields = {'schema_version', 'sequence', 'nonce', 'binding_digest', 'directory',
                  'deadline_monotonic', 'output_limit', 'phase'}
        if sequence < 12:
            fields |= {'protocol_version', 'transport'}
            _require(type(entry.get('schema_version')) is int and entry['schema_version'] == 2
                     and type(entry.get('protocol_version')) is int and entry['protocol_version'] == 2
                     and entry.get('transport') == 'app_server' and entry.get('phase') == 'app_server',
                     'Original fresh stage transport differs')
        else:
            _require(type(entry.get('schema_version')) is int and entry['schema_version'] == 1
                     and entry.get('phase') == 'version', 'Original last stage is not version-only')
        _require(set(entry) == fields and type(entry.get('sequence')) is int and entry['sequence'] == sequence
                 and isinstance(entry.get('nonce'), str) and len(entry['nonce']) == 32
                 and all(c in '0123456789abcdef' for c in entry['nonce'])
                 and path.name == f"{sequence:02d}-{entry['nonce']}"
                 and entry['binding_digest'] == digest(binding), 'Original stage identity differs')
        _require(set(finished) == {'entry_digest', 'spawn_failed', 'stop'}
                 and finished['entry_digest'] == digest(entry) and finished['spawn_failed'] is False,
                 'Failure requires each actual process to have an original saved stop')
        validate_stop(finished['stop'], entry, started)
        stopped = finished['stop']
        _require(stopped['invocation_started'] is True and stopped['exit_code'] == 0
                 and stopped['reason'] is None, 'Fresh/version process did not complete')
        expected_stop = read_metadata(fresh_paths[sequence - 1]) if sequence < 12 else version_stop
        _require(stopped == expected_stop, 'Original journal differs from raw fresh/version stop')
        if sequence == 12:
            raw_version = b'codex-cli 0.145.0\n'
            _require(stopped['stdout_bytes'] == len(raw_version)
                     and stopped['stdout_sha256'] == hashlib.sha256(raw_version).hexdigest()
                     and stopped['response_saved'] is False and stopped['response_sha256'] is None
                     and stopped['response_error'] is None, 'Pinned version-only output differs')
        descriptors.append({'sequence': sequence, 'nonce': entry['nonce'],
            'prepared_digest': digest(entry), 'started_digest': digest(started),
            'finished_digest': digest(finished)})
    return descriptors


def _pre_dispatch_envelope(*, failure_phase, error_code, receipt, identity, entry_permit,
                           binding, fresh_assessment, version_stop, journal_records):
    from .provider_app_server_business_identity import BUSINESS_DISPATCH_TRANSPORT_VERSION
    from .provider_native_transport_identity import native_profile_digest
    from .provider_schema import ProviderUsage
    _require(failure_phase == PRE_DISPATCH_FAILURE_PHASE and error_code in PRE_DISPATCH_CONTEXT_ERRORS,
             'Failure is outside the specific pre-auth context validation phase')
    checked = ProviderReceipt.model_validate(receipt).model_dump()
    _require(canonical_json(checked) == canonical_json(receipt)
             and checked['fixture'] is False and checked['invocation_started'] is False
             and checked['process_stopped'] is True and checked['exit_code'] is None
             and checked['cli_version'] == '0.145.0' and checked['requested_model'] == 'gpt-5.5'
             and checked['reported_model'] is None and checked['response_sha256'] is None
             and checked['stdout_bytes'] == checked['stderr_bytes'] == 0
             and checked['usage'] == ProviderUsage().model_dump(), 'Pre-dispatch receipt differs')
    _require(entry_permit.get('dispatch_intent_digest') is None
             and entry_permit.get('proposal_id') == binding['proposal_id'] == checked['proposal_id']
             and entry_permit.get('attempt_id') == binding['attempt_id'] == checked['attempt_id']
             and entry_permit.get('prepared_digest') == binding['prepared_digest']
             and entry_permit.get('profile_digest') == native_profile_digest(identity)
             and entry_permit.get('config_digest') == identity['profile']['config_digest'],
             'Failure lacks its exact unconsumed original permit')
    _require(fresh_assessment.get('read_verified') is True and fresh_assessment.get('write_verified') is True
             and fresh_assessment.get('business_qualification') is False
             and fresh_assessment.get('auth_context_equivalence_claimed') is False,
             'Failure lacks complete non-business synthetic boundary evidence')
    value = {'schema_version': 1, 'proof_kind': PRE_DISPATCH_FAILURE_KIND,
        'transport_version': BUSINESS_DISPATCH_TRANSPORT_VERSION,
        'failure_phase': failure_phase, 'error_code': error_code,
        'proposal_id': checked['proposal_id'], 'attempt_id': checked['attempt_id'],
        'identity_digest': wire_digest(identity), 'profile_digest': native_profile_digest(identity),
        'config_digest': entry_permit['config_digest'], 'prepared_digest': binding['prepared_digest'],
        'entry_permit_digest': digest(entry_permit), 'attempt_binding_digest': digest(binding),
        'fresh_report_digest': fresh_assessment['report_digest'], 'version_stop_digest': digest(version_stop),
        'journal_records': journal_records, 'official_native_processes': 12,
        'business_invocations': 0, 'backend_model_calls': 0, 'auth_operations': 0,
        'dispatch_issued': False, 'receipt_digest': digest(checked)}
    return {**value, 'proof_digest': digest(value)}


def write_app_server_pre_dispatch_failure(transport_dir, *, failure_phase, error_code, receipt,
        identity, entry_permit, journal_root, expected_attempt_binding, fresh_assessment, version_stop):
    """Write one new, narrow metadata receipt; does not authorize or execute I/O tools.

    Called by the original Native leaf only after all bounded receipt and stop
    files are durably saved. Existing files are never replaced or backfilled.
    Saved verification still independently replays current fresh raw evidence
    and the permanent original authority, rather than trusting this envelope.
    """
    from pathlib import Path
    from .provider_supervision_journal import _write
    root = Path(transport_dir)
    native = read_metadata(root / 'native-evidence.json', 768 * 1024)
    saved = read_metadata(root / 'receipt.json')
    _require(saved['receipt'] == receipt and saved['error']['code'] == error_code
             and native.get('identity') == identity
             and native.get('research_authorization_entry') == entry_permit
             and native.get('fresh_assessment') == fresh_assessment
             and read_metadata(root / 'version-supervision.json') == version_stop,
             'Writer inputs differ from the original saved failure')
    fresh_root = Path(native['fresh_evidence']['report_path']).parent
    records = _pre_dispatch_journal(journal_root, expected_attempt_binding,
                                   fresh_root=fresh_root, version_stop=version_stop)
    value = _pre_dispatch_envelope(failure_phase=failure_phase, error_code=error_code,
        receipt=receipt, identity=identity, entry_permit=entry_permit, binding=expected_attempt_binding,
        fresh_assessment=fresh_assessment, version_stop=version_stop, journal_records=records)
    _write(root / 'pre-dispatch-failure.json', value)
    return value


def verify_app_server_pre_dispatch_failure(store, proposal, attempt, *, expected_entry_permit,
                                           connection=None):
    """Observe a new exact pre-auth failure, never qualify a success or another call."""
    from contextlib import nullcontext
    from .proposal_requests import ProposalRequests
    from .provider_research_schema import CONTRACT_ID
    from .provider_native_transport_identity import current_native_identity
    from .provider_app_server_qualification import verify_app_server_fresh_evidence
    from .codex_cli_provider import _bounded_private_file
    from pathlib import Path
    import os
    try:
        request = proposal['request']
        _require(request.get('schema_version') == 2 and request.get('contract_id') == CONTRACT_ID
                 and request.get('operation') == 'generate_candidates'
                 and request['source']['kind'] in {'research_revision', 'accepted_material'},
                 'Pre-dispatch proof requires the original Loop generation scope')
        _require(attempt['id'] == proposal['current_attempt_id'] and attempt['prepared_digest'] is not None
                 and attempt.get('status') == 'failed' and proposal.get('status') == 'failed',
                 'Pre-dispatch proof requires the exact failed original attempt')
        config = ProviderConfig.model_validate(proposal['config'])
        _require(native_transport(config) == 'app_server' and config.mode == 'codex_cli'
                 and config.model == 'gpt-5.5' and config.reasoning_effort == 'low'
                 and config.expected_cli_version == '0.145.0', 'Pre-dispatch configuration differs')
        _require(expected_entry_permit.get('config_digest') == wire_digest(config.model_dump()),
                 'Failure permit config digest differs')
        requests = ProposalRequests(store, configs={})
        folder = requests._folder(proposal['id']) / 'attempts' / attempt['id']
        transport = folder / 'transport'
        envelope = read_metadata(transport / 'pre-dispatch-failure.json')
        for path in [transport / name for name in ('dispatch-intent.json', 'invocation.json',
                     'auth-supervision.json', 'real-supervision.json', 'app-server-capture',
                     'response.raw.json')] + [folder / name for name in ('response.raw.json', 'parsed.json')]:
            _require(not os.path.lexists(path), 'Pre-dispatch failure contains auth/dispatch/generation artifacts')
        identity, profile = current_native_identity(config, contract_id=CONTRACT_ID)
        _require(profile['static_checks']['passed'] is True, 'Current failure profile is unavailable')
        native = read_metadata(transport / 'native-evidence.json', 768 * 1024)
        _require(read_metadata(transport / 'profile.json', 768 * 1024) == profile
                 and native.get('identity') == identity
                 and native.get('transport_version') == identity['transport_version']
                 and native.get('context_policy_version') == identity['context_policy_version']
                 and native.get('contract_id') == CONTRACT_ID
                 and native.get('host_authorized') is True and native.get('automatic_provider_retry') is False
                 and native.get('dispatch_started') is False
                 and not any(k in native for k in ('native_authentication_confirmed',
                     'research_authorization_dispatch', 'invocation_digest', 'capture'))
                 and native.get('research_authorization_entry') == expected_entry_permit,
                 'Failure differs from its current unconsumed entry permit')
        handler = request_handler(request)
        body = handler.parse_request(request)
        prepared = read_metadata(folder / 'prepared.json', 512 * 1024)
        expected_prepared = {'proposal_id': proposal['id'], 'attempt_id': attempt['id'],
            'context_digest': proposal['context_digest'],
            'input_text': handler.build_prompt(body, proposal['context'], proposal['id'], attempt['id']),
            'output_schema': handler.output_schema(proposal['context']), 'provider_config': proposal['config'],
            'budget': request['budget']}
        _require(prepared == expected_prepared and digest(prepared) == attempt['prepared_digest']
                 and _bounded_private_file(transport / 'input.txt', body.budget.max_context_bytes)
                     == prepared['input_text'].encode('utf-8')
                 and read_metadata(transport / 'output.schema.json', 512 * 1024) == prepared['output_schema'],
                 'Failure input/schema differs from original prepared context')
        saved = read_metadata(transport / 'receipt.json')
        _require(saved == {'receipt': attempt['receipt'], 'error': attempt['error']}
                 and saved['error']['code'] in PRE_DISPATCH_CONTEXT_ERRORS
                 and saved['error'].get('retryable') is False, 'Failure receipt/error differs')
        fresh = verify_app_server_fresh_evidence(native, config=config, expected_current_identity=identity)
        _require(native.get('fresh_assessment') == fresh
                 and native.get('boundary_checks') == {'read': {'process_stopped': True, 'verified': True},
                                                      'write': {'process_stopped': True, 'verified': True}},
                 'Failure boundary summary differs from full original fresh evidence')
        with (nullcontext(connection) if connection is not None else closing(connect(store.db_path))) as opened:
            origin_row = opened.execute('SELECT payload,digest FROM provider_attempt_origins WHERE attempt_id=?',
                                        (attempt['id'],)).fetchone()
            workspace_row = opened.execute("SELECT value FROM settings WHERE key='provider_workspace_identity'").fetchone()
            _require(origin_row is not None and workspace_row is not None, 'Failure lacks original charged origin')
            origin = strict_json(origin_row['payload'].encode(), max_bytes=65536)
            _require(digest(origin) == origin_row['digest'], 'Failure origin digest differs')
            # finish() has a just-updated DTO; use it for the original attempt, not stale running state.
            value = {**proposal, 'attempts': [attempt]}
            proof = termination_proof(store.root, value, workspace_row[0], origin)
        _require(proof.get('confirmed') is True and proof.get('reason') == 'BOUND_STOP_EVIDENCE'
                 and len(proof.get('stages', [])) == 12
                 and all(item.get('kind') == 'saved_stop' for item in proof['stages']),
                 'Failure lacks twelve complete original saved stop records')
        binding = read_metadata(folder / 'supervision' / 'binding.json')
        _require(binding['workspace_identity'] == origin['workspace_identity']
                 and binding['host_boot'] == origin['host_boot'], 'Failure binding differs from its original origin')
        version_stop = read_metadata(transport / 'version-supervision.json')
        version_summary = native.get('supervision_metadata')
        _require(type(version_summary) is dict and set(version_summary) == {'version', 'auth', 'real'}
                 and version_summary['auth'] == {'saved': False} and version_summary['real'] == {'saved': False}
                 and version_summary['version'].get('saved') is True,
                 'Failure metadata contains unexpected authentication or generation')
        records = _pre_dispatch_journal(folder / 'supervision', binding,
            fresh_root=Path(native['fresh_evidence']['report_path']).parent, version_stop=version_stop)
        expected = _pre_dispatch_envelope(failure_phase=PRE_DISPATCH_FAILURE_PHASE,
            error_code=saved['error']['code'], receipt=saved['receipt'], identity=identity,
            entry_permit=expected_entry_permit, binding=binding, fresh_assessment=fresh,
            version_stop=version_stop, journal_records=records)
        _require(canonical_json(envelope) == canonical_json(expected),
                 'Closed pre-dispatch envelope differs from original current evidence')
        _require(current_native_identity(config, contract_id=CONTRACT_ID)[0] == identity,
                 'Source changed during pre-dispatch failure verification')
        return {**expected, 'supervision_digest': digest(proof), 'authority_id': 'research_loop_native_v1',
                'supplier_cost': None, 'semantic_fidelity': 'unverified'}
    except ResearchQualificationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ResearchQualificationError('Exact pre-dispatch failure evidence is unavailable or inconsistent') from exc
