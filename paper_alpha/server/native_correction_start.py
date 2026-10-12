"""Managed locators for host-prepared loops over original immutable authority.

Only prepare_native_start is a local host mutation. HTTP support/start are read
and attach operations. They cannot mint, reserve, dispatch or replace a task.
"""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import stat
import tempfile

from ..storage import digest, json_text
from .db import connect, transaction
from .native_correction_start_schema import (ManagedNativeCatalog, ManagedNativePlan,
    NativeCorrectionPrepare, NativeCorrectionSupport, NativeCorrectionStartSummary)
from .proposal_requests import ProposalServiceError
from .provider_contract import digest as wire_digest, strict_json
from .provider_correction_native import CORRECTION_INSTRUCTION
from .provider_host_authority import _workspace
from .provider_loop_native_authority import LoopNativeResearchAuthority, create_loop_native_grant
from .provider_research_context import resolve_research_source
from .provider_research_schema import ResearchProposalCreate, CONTRACT_ID
from .research_correction_policy import policy_digest
from .research_loop_correction import _historical_source
from .research_loops import ResearchLoops
from .research_loops_schema import parse_loop_create
from .research_sessions import ResearchSessions
from .service import ServiceError

CATALOG_ENV = 'PAPER_ALPHA_NATIVE_CORRECTION_CATALOG'
MAX_FILE_BYTES = 256 * 1024
POLICY_ID = 'equivalent-expression-representation-v2'
_ROOT = Path(__file__).resolve().parent
_SERVICE_FILES = ('native_correction_start.py', 'native_correction_start_schema.py',
                  'provider_loop_api.py', 'provider_authority_router.py',
                  'provider_research_api.py', 'api.py', 'launcher.py')


def service_source_digest():
    return digest({name: hashlib.sha256((_ROOT / name).read_bytes()).hexdigest()
                   for name in _SERVICE_FILES})


def _qualify_read(config, report):
    """Verify existing synthetic evidence against current native identities.

    This never runs the probe or reads authentication. A copied ready flag has
    no authority; the original grant and per-attempt dynamic gates still apply.
    """
    if config.mode == 'fixture':
        return None
    try:
        if config.native_transport == 'app_server':
            from .provider_native_transport_identity import current_native_identity
            from .provider_app_server_qualification import verify_app_server_fresh_evidence
            identity, _ = current_native_identity(config, contract_id=CONTRACT_ID)
            result = verify_app_server_fresh_evidence({'fresh_evidence': report},
                config=config, expected_current_identity=identity)
            _need(result['read_verified'] and result['write_verified'], code='READ_BOUNDARY_UNVERIFIED')
            # The catalog saves only this bounded locator; every prepare,
            # inspect and attach re-reads all fixed raw files at its location.
            return digest(report)
        from .provider_read_probe import assess_report, OUTPUT_SCHEMA
        from .provider_read_boundary import READ_BOUNDARY_POLICY_DIGEST, READ_BOUNDARY_POLICY_VERSION
        from .provider_native import _identity, _boundaries_match
        identity, profile = _identity(config, contract_id=CONTRACT_ID)
        assessment = assess_report(report, allow_apply_patch=True)
        _need(type(report) is dict and report.get('challenge_version') == 3
              and report.get('policy_version') == READ_BOUNDARY_POLICY_VERSION
              and report.get('policy_digest') == READ_BOUNDARY_POLICY_DIGEST
              and report.get('allow_apply_patch') is True
              and report.get('config_digest') == wire_digest(config.model_dump())
              and report.get('schema_digest') == wire_digest(OUTPUT_SCHEMA)
              and report.get('source_sha256') == identity['source_digests']['paper_alpha/server/provider_read_probe.py']
              and report.get('helper_sha256') == identity['source_digests']['paper_alpha/server/provider_read_boundary.py']
              and assessment.get('read_boundary_observed') is True
              and _boundaries_match(report, 'read', identity, profile)
              and report.get('process_stopped') is True and report.get('error') is None
              and report.get('real_model_invocations') == 0 and report.get('real_dispatch_enabled') is False
              and all(c.get('executable_sha256') == profile['executable']['sha256']
                      and c.get('config_digest') == identity['config_digest'] for c in report['cases']),
              'Current full-matrix Native read boundary is unverified', 'READ_BOUNDARY_UNVERIFIED')
        return digest(report)
    except (ImportError, KeyError, TypeError, ValueError, OSError, ServiceError) as exc:
        raise ProposalServiceError('Current full-matrix Native read boundary is unverified', 409,
                                   code='READ_BOUNDARY_UNVERIFIED') from exc


def _need(test, message='Managed Native plan failed integrity verification', code='NATIVE_PLAN_INTEGRITY'):
    if not test:
        raise ProposalServiceError(message, 409, code=code)


def _unlinked_absolute(path):
    path = Path(path)
    _need(path.is_absolute(), 'Managed catalog must be an absolute host path', 'NATIVE_CATALOG_INVALID')
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        _need(not current.is_symlink(), 'Managed catalog cannot contain symlinks', 'NATIVE_CATALOG_INVALID')
    return path


def _directory(path):
    path = _unlinked_absolute(path)
    _need(path.is_dir(), 'Managed catalog directory is missing', 'NATIVE_CATALOG_MISSING')
    return path


def _read_file(directory, name):
    _need('/' not in name and '\\' not in name and name not in {'.', '..'}, code='NATIVE_CATALOG_INVALID')
    root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _need(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE_BYTES,
                  'Managed catalog entry is not a bounded regular file', 'NATIVE_CATALOG_INVALID')
            raw = stream.read(MAX_FILE_BYTES + 1)
        return strict_json(raw, max_bytes=MAX_FILE_BYTES), hashlib.sha256(raw).hexdigest()
    finally:
        os.close(root_fd)


def _atomic(directory, name, value):
    raw = (json_text(value) + '\n').encode()
    _need(len(raw) <= MAX_FILE_BYTES, code='NATIVE_CATALOG_INVALID')
    fd, temporary = tempfile.mkstemp(prefix='.native-start-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
        root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ManagedNativeStarts:
    """Startup-selected catalog with file pins and original-ledger validation."""
    def __init__(self, store, registry, catalog=None):
        self.store, self.registry = store, registry
        self.proposals = registry.proposal_requests(store)
        self.loops = ResearchLoops(store, self.proposals)
        self.authority = self.proposals.research_authority
        self.directory = None
        self.entries, self.file_pins = {}, {}
        self.error = ('NATIVE_CATALOG_MISSING', 'Host has not enabled a managed Native start catalog.')
        if catalog is None:
            return
        try:
            self.directory = _directory(catalog)
            value, pin = _read_file(self.directory, 'catalog.json')
            parsed = ManagedNativeCatalog.model_validate(value)
            _need(len({p.start_id for p in parsed.plans}) == len(parsed.plans), code='NATIVE_CATALOG_INVALID')
            self.file_pins['catalog.json'] = pin
            for entry in parsed.plans:
                value, pin = _read_file(self.directory, entry.start_id + '.json')
                plan = ManagedNativePlan.model_validate(value).model_dump()
                _need(digest(plan) == entry.start_digest and entry.start_id == 'native_start_' + digest(plan),
                      code='NATIVE_PLAN_INTEGRITY')
                self.entries[entry.start_id] = plan
                self.file_pins[entry.start_id + '.json'] = pin
                if plan['read_qualification_digest'] is not None:
                    report, pin = _read_file(self.directory, entry.start_id + '.read-qualification.json')
                    _need(digest(report) == plan['read_qualification_digest'], code='READ_BOUNDARY_UNVERIFIED')
                    self.file_pins[entry.start_id + '.read-qualification.json'] = pin
            self.error = None
        except (ValueError, OSError, ServiceError) as exc:
            self.entries = {}
            self.error = (getattr(exc, 'code', 'NATIVE_CATALOG_INVALID'), 'Managed Native catalog is unavailable or failed verification.')

    def _files(self):
        if self.error:
            raise ProposalServiceError(self.error[1], 409, code=self.error[0])
        _directory(self.directory)
        for name, expected in self.file_pins.items():
            _, current = _read_file(self.directory, name)
            _need(current == expected, 'Managed catalog changed after service startup', 'NATIVE_CATALOG_CHANGED')

    def _records(self, connection, plan):
        _need(plan['service_source_digest'] == service_source_digest(),
              'Workbench source changed after host preparation', 'NATIVE_START_SOURCE_CHANGED')
        _need(plan['workspace_id'] == _workspace(connection), code='LOOP_NATIVE_WORKSPACE_MISMATCH')
        leaf = self.authority.leaf_for_session(plan['session_id'], connection=connection)
        _need(type(leaf) is LoopNativeResearchAuthority, 'Managed start has no concrete Native owner', 'NATIVE_START_WRONG_AUTHORITY')
        binding, grant = leaf._binding(connection, plan['session_id'])
        loaded = self.loops._load(connection, plan['loop_id'])
        request = loaded['request']
        initial = request['initial_proposal']
        source = initial['source']
        _need(binding['grant_id'] == plan['grant_id'] == grant['id']
              and digest(grant['request']) == plan['grant_request_digest']
              and digest(grant['profile']) == plan['grant_profile_digest']
              and grant['request']['program_id'] == plan['program_id']
              and grant['request']['plan_digest'] == plan['loop_request_digest']
              and digest(request) == plan['loop_request_digest']
              and loaded['row']['session_id'] == plan['session_id'] == initial['session_id']
              and initial['provider_config_id'] == plan['provider_config_id']
              and wire_digest(grant['config']) == plan['provider_config_digest']
              and initial['expected_source_digest'] == plan['source_digest'] == binding['root_source_digest']
              and source == {'kind': 'research_revision', 'research_id': plan['research_id'],
                             'base_revision_id': plan['base_revision_id']}
              and request['schema_version'] == 2 and request['correction_mode'] == 'provider_optional'
              and request['max_repairs'] == 1
              and request['correction_policy_id'] == plan['correction_policy_id'] == POLICY_ID
              and request['correction_policy_digest'] == plan['correction_policy_digest'] == policy_digest(POLICY_ID))
        session = loaded['loaded_session']
        _need(session['request']['budget']['max_provider_dispatches'] == 2
              and session['request']['budget']['max_experiments'] == int(request['allow_experiment'])
              and session['request']['config']['provider_config_digest'] == plan['provider_config_digest'])
        specs = grant['request']['slots']
        generation = [s for s in specs if s['operation'] == 'generate_candidates']
        repairs = [s for s in specs if s.get('correction_version') == 1]
        _need(len(specs) == 2 and len(generation) == len(repairs) == 1
              and generation[0]['call_id'] == 'C01' and repairs[0]['call_id'] == 'C02'
              and generation[0]['effect_key'] == initial['idempotency_key']
              and generation[0]['instruction_digest'] == digest(initial['instruction'])
              and repairs[0]['effect_key'] == ResearchLoops._effect_key(plan['loop_id'], 3)
              and repairs[0]['instruction_digest'] == digest(CORRECTION_INSTRUCTION)
              and repairs[0]['target_from_call_id'] == 'C01')
        return leaf, grant, loaded

    def inspect(self, start_id):
        self._files()
        plan = self.entries.get(start_id)
        _need(plan is not None, 'Approved Native plan is not in this service catalog', 'NATIVE_START_NOT_FOUND')
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            leaf, grant, loaded = self._records(connection, plan)
            available, code, message = True, 'NATIVE_START_AVAILABLE', 'Attach this exact bounded host-approved development task.'
            state = 'prepared' if not loaded['actions'] else 'active'
            session = self.loops.sessions._view(loaded['loaded_session'])
            children, _ = self.loops._children(connection, loaded)
            if any(p['status'] == 'interrupted' or any(a['status'] == 'interrupted' for a in p['attempts']) for p in children):
                state, available, code = 'unknown', False, 'NATIVE_START_UNKNOWN'
                message = 'The original task has an uncertain attempt; inspect its saved receipt before any action.'
            elif session['effective_state'] in {'completed', 'stopped', 'cancelled', 'exhausted', 'interrupted'}:
                state, available, code = 'terminal', False, 'NATIVE_START_TERMINAL'
                message = 'This original task is terminal; its saved loop remains readable.'
            try:
                if grant['config']['mode'] != 'fixture':
                    report, _ = _read_file(self.directory, start_id + '.read-qualification.json')
                    _need(digest(report) == plan['read_qualification_digest'], code='READ_BOUNDARY_UNVERIFIED')
                    _qualify_read(self.registry.configs[plan['provider_config_id']], report)
                leaf.qualify_correction_mode(connection, plan['session_id'], POLICY_ID, plan['correction_policy_digest'])
                self.loops.sessions._resolve(connection, loaded['loaded_session']['state']['current_source'])
                historical, _ = _historical_source(self.store, connection, loaded['request']['initial_proposal']['source'])
                _need(digest(historical) == plan['source_digest'], code='NATIVE_START_SOURCE_CHANGED')
                _need(session['remaining_seconds'] > 0, 'Original session expired', 'SESSION_EXPIRED')
            except (ServiceError, ValueError, OSError) as exc:
                available, code = False, getattr(exc, 'code', 'NATIVE_START_UNAVAILABLE')
                message = 'This exact host plan is blocked: ' + code + '.'
            observed = any(a.get('receipt') and a['status'] in {'ready', 'rejected', 'abstained'}
                           for p in children for a in p['attempts'])
            summary = {**{key: plan[key] for key in ('schema_version', 'research_id', 'base_revision_id',
                'provider_config_id', 'provider_config_digest', 'source_digest', 'session_id', 'loop_id',
                'loop_request_digest', 'correction_policy_id', 'correction_policy_digest')},
                'start_id': start_id, 'start_digest': digest(plan), 'state': state,
                'available': available, 'reason_code': code, 'message': message,
                'authorization': {'authority_id': grant['authority_id'], 'grant_id': grant['id'],
                    'kind': grant['request']['authorization_kind'],
                    'human_authorization_digest': grant['request']['human_authorization_digest'],
                    'deadline_at': grant['deadline_at'], 'max_provider_dispatches': 2,
                    'max_repairs': 1, 'candidate_limit': 1},
                'evidence': {'profile_digest': digest(grant['profile']), 'profile_status': 'pinned_static',
                    'boundary_status': 'fixture_only' if grant['config']['mode'] == 'fixture' else 'per_attempt_verification_required',
                    'inference_status': ('fixture_observed' if grant['config']['mode'] == 'fixture' else 'native_observed') if observed else 'unverified',
                    'semantic_fidelity': 'unverified'},
                'development_scope': 'ordinary_synthetic_task1_generation1_eligible_repair1',
                'public_projection_redacted': False}
            return NativeCorrectionStartSummary.model_validate(summary).model_dump()

    def support(self, body):
        identity = body.model_dump()
        result = {**identity, 'available': False, 'reason_code': 'NATIVE_START_NOT_FOUND',
                  'message': 'No approved Native task matches this exact source and provider.',
                  'plans': [], 'public_projection_redacted': False}
        try:
            config = self.registry.configs.get(body.provider_config_id)
            if config is not None and config.mode != 'fixture' and not self.entries:
                raise ProposalServiceError('No matching verified full-matrix read qualification is installed.', 409,
                                           code='READ_BOUNDARY_UNVERIFIED')
            self._files()
            for start_id, plan in self.entries.items():
                if all(plan[key] == identity[key] for key in ('research_id', 'base_revision_id', 'provider_config_id')):
                    result['plans'].append(self.inspect(start_id))
            result['available'] = any(p['available'] for p in result['plans'])
            if result['plans']:
                selected = next((p for p in result['plans'] if p['available']), result['plans'][0])
                result.update(reason_code=selected['reason_code'], message=selected['message'])
            elif config is not None and config.mode != 'fixture':
                result.update(reason_code='READ_BOUNDARY_UNVERIFIED',
                    message='No matching verified full-matrix Native read qualification is installed.')
        except (ValueError, OSError, ServiceError) as exc:
            result.update(reason_code=getattr(exc, 'code', 'NATIVE_CATALOG_INVALID'),
                          message='Managed Native catalog or original plan failed verification.', plans=[])
        return NativeCorrectionSupport.model_validate(result).model_dump()

    def start(self, body):
        self._files()
        plan = self.entries.get(body.start_id)
        _need(plan is not None, 'Approved Native plan was not found', 'NATIVE_START_NOT_FOUND')
        _need(body.start_digest == digest(plan) and all(getattr(body, key) == plan[key]
              for key in ('research_id', 'base_revision_id', 'provider_config_id')),
              'Native start identity differs from the approved plan', 'NATIVE_START_IDENTITY_MISMATCH')
        summary = self.inspect(body.start_id)
        _need(summary['available'], summary['message'], summary['reason_code'])
        # All starts attach the already-created loop. The supplied durable key
        # is echoed for client recovery; it cannot create a second task/effect.
        return summary, self.loops.get(plan['loop_id'])


def prepare_native_start(store, registry, spec, catalog, *, human_authorization_digest=None, read_qualification=None):
    """Local, no-dispatch host preparation. Publication follows all DB checks."""
    spec = NativeCorrectionPrepare.model_validate(spec)
    config = registry.ensure_dispatchable(spec.provider_config_id)
    _need(config.native_transport == 'app_server' or spec.app_server_attempt_seconds is None,
          'App Server attempt budget cannot be used by another transport', 'NATIVE_PROFILE_UNSUPPORTED')
    attempt_seconds = spec.app_server_attempt_seconds or spec.proposal_seconds
    report = None
    qualification_digest = None
    if config.mode != 'fixture':
        _need(human_authorization_digest is not None, 'Real preparation requires the original scoped user-message record digest',
              'LOOP_NATIVE_REAL_AUTHORIZATION_REQUIRED')
        _need(read_qualification is not None, 'Current Native read boundary needs matching full-matrix evidence',
              'READ_BOUNDARY_UNVERIFIED')
        report, _ = _read_file(_directory(Path(read_qualification).parent), Path(read_qualification).name)
        qualification_digest = _qualify_read(config, report)
    catalog = _unlinked_absolute(catalog)
    catalog.mkdir(parents=True, exist_ok=True)
    catalog = _directory(catalog)
    if (catalog / 'catalog.json').exists() or (catalog / 'catalog.json').is_symlink():
        value, _ = _read_file(catalog, 'catalog.json')
        prior_catalog = ManagedNativeCatalog.model_validate(value)
        _need(len({p.start_id for p in prior_catalog.plans}) == len(prior_catalog.plans), code='NATIVE_CATALOG_INVALID')
        for entry in prior_catalog.plans:
            value, _ = _read_file(catalog, entry.start_id + '.json')
            prior_plan = ManagedNativePlan.model_validate(value).model_dump()
            _need(digest(prior_plan) == entry.start_digest and entry.start_id == 'native_start_' + digest(prior_plan))
    proposals = registry.proposal_requests(store)
    with proposals._lock('native-start-prepare-' + digest(spec.program_id)):
        descriptor = {'kind': 'research_revision', 'research_id': spec.research_id,
                      'base_revision_id': spec.base_revision_id}
        with closing(connect(store.db_path)) as connection:
            connection.execute('BEGIN')
            actual, _ = _historical_source(store, connection, descriptor)
        private = resolve_research_source(store, descriptor)
        _need(digest(private) == digest(actual), code='NATIVE_START_SOURCE_CHANGED')
        root = {'kind': 'research_revision', 'research_id': spec.research_id, 'revision_id': spec.base_revision_id,
                'revision_digest': private['revision']['digest'], 'source_digest': digest(private)}
        sessions = ResearchSessions(store)
        session = sessions.create(root, spec.purpose,
            {'provider_config_id': config.config_id, 'provider_config_digest': wire_digest(config.model_dump()), 'contract_id': CONTRACT_ID},
            {'max_provider_dispatches': 2, 'max_tool_actions': 8, 'max_experiments': int(spec.allow_experiment),
             'max_failures': 3, 'max_seconds': spec.max_seconds}, spec.program_id + '-session')
        initial = ResearchProposalCreate(operation='generate_candidates', source=descriptor, session_id=session['id'],
            expected_session_digest=session['state_digest'], expected_source_digest=root['source_digest'],
            provider_config_id=config.config_id, instruction=spec.generation_instruction,
            budget={'max_provider_invocations': 1, 'max_seconds': attempt_seconds},
            idempotency_key=spec.program_id + '-generation')
        request = parse_loop_create({'schema_version': 2, 'initial_proposal': initial.model_dump(), 'max_repairs': 1,
            'allow_experiment': spec.allow_experiment,
            'experiment_budget': {'max_steps': 10, 'max_failures': 2, 'max_seconds': 180} if spec.allow_experiment else None,
            'correction_policy_id': POLICY_ID, 'correction_policy_digest': policy_digest(POLICY_ID),
            'correction_mode': 'provider_optional', 'idempotency_key': spec.program_id + '-loop'}).model_dump()
        loop_id = 'research_loop_' + digest(request)
        grant = create_loop_native_grant(store, registry, program_id=spec.program_id, plan_digest=digest(request),
            provider_config_id=config.config_id, max_seconds=spec.max_seconds + 30,
            max_proposal_seconds=attempt_seconds,
            authorization_kind='fixture' if config.mode == 'fixture' else 'bounded_qualification',
            human_authorization_digest=human_authorization_digest,
            idempotency_key=spec.program_id + '-grant',
            slots=[{'call_id': 'C01', 'effect_key': initial.idempotency_key, 'operation': 'generate_candidates',
                'source_kind': 'research_revision', 'root_source_digest': root['source_digest'],
                'instruction_digest': digest(initial.instruction), 'candidate_limit': 1},
                {'call_id': 'C02', 'effect_key': ResearchLoops._effect_key(loop_id, 3), 'operation': 'repair_candidate',
                 'source_kind': 'research_revision', 'root_source_digest': root['source_digest'], 'target_from_call_id': 'C01',
                 'instruction_digest': digest(CORRECTION_INSTRUCTION), 'candidate_limit': 1, 'correction_version': 1,
                 'policy_id': POLICY_ID, 'policy_digest': policy_digest(POLICY_ID)}])
        leaf = LoopNativeResearchAuthority(store, registry)
        with transaction(store.db_path) as connection:
            leaf.bind_session(connection, grant['id'], session['id'])
        loops = ResearchLoops(store, proposals)
        loop = loops.create(request)
        _need(not loop['actions'] and not loop['proposals'] and loop['session']['usage']['provider_dispatches'] == 0,
              'Preparation replay cannot adopt an already used task', 'NATIVE_START_ALREADY_USED')
        plan = ManagedNativePlan.model_validate({'schema_version': 1, 'program_id': spec.program_id,
            'research_id': spec.research_id, 'base_revision_id': spec.base_revision_id,
            'provider_config_id': config.config_id, 'provider_config_digest': wire_digest(config.model_dump()),
            'source_digest': root['source_digest'], 'session_id': session['id'], 'loop_id': loop['id'],
            'loop_request_digest': digest(request), 'grant_id': grant['id'], 'grant_request_digest': digest(grant['request']),
            'grant_profile_digest': digest(grant['profile']), 'workspace_id': grant['workspace_id'],
            'correction_policy_id': POLICY_ID, 'correction_policy_digest': policy_digest(POLICY_ID),
            'service_source_digest': service_source_digest(), 'read_qualification_digest': qualification_digest}).model_dump()
        start_id, start_digest = 'native_start_' + digest(plan), digest(plan)
        with proposals._lock('native-start-catalog-' + digest(str(catalog))):
            if (catalog / 'catalog.json').exists() or (catalog / 'catalog.json').is_symlink():
                existing, _ = _read_file(catalog, 'catalog.json')
                entries = ManagedNativeCatalog.model_validate(existing).model_dump()['plans']
            else:
                entries = []
            entry = {'start_id': start_id, 'start_digest': start_digest}
            if entry not in entries:
                _need(len(entries) < 32, 'Managed catalog is full', 'NATIVE_CATALOG_FULL')
                entries.append(entry)
            filename = start_id + '.json'
            if report is not None:
                _atomic(catalog, start_id + '.read-qualification.json', report)
            if (catalog / filename).exists() or (catalog / filename).is_symlink():
                existing, _ = _read_file(catalog, filename)
                _need(existing == plan, code='NATIVE_PLAN_INTEGRITY')
            else:
                _atomic(catalog, filename, plan)
            _atomic(catalog, 'catalog.json', {'schema_version': 1, 'plans': entries})
        return ManagedNativeStarts(store, registry, catalog).inspect(start_id)
