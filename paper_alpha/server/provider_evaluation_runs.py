"""Host-only G4 evaluation ownership; original queues remain the execution authority."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from ..storage import digest, json_text
from ..provider_evaluation_v2_schema import parse_execution_plan
from .db import connect, transaction
from .service import ServiceError

SCHEMA = """
CREATE TABLE provider_evaluation_runs(id TEXT PRIMARY KEY,plan_digest TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,deadline_at TEXT NOT NULL,qualification TEXT);
CREATE TABLE provider_evaluation_cases(evaluation_id TEXT NOT NULL REFERENCES provider_evaluation_runs(id),case_id TEXT NOT NULL,session_id TEXT NOT NULL UNIQUE REFERENCES research_sessions(id),payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(evaluation_id,case_id));
CREATE TABLE provider_evaluation_attempts(id TEXT PRIMARY KEY,evaluation_id TEXT NOT NULL REFERENCES provider_evaluation_runs(id),case_id TEXT NOT NULL,proposal_id TEXT NOT NULL UNIQUE REFERENCES proposal_requests(id),attempt_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,digest TEXT NOT NULL,UNIQUE(evaluation_id,case_id));
CREATE TABLE provider_evaluation_engines(evaluation_id TEXT NOT NULL REFERENCES provider_evaluation_runs(id),slot TEXT NOT NULL,job_id TEXT NOT NULL UNIQUE REFERENCES research_jobs(id),payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(evaluation_id,slot));
"""


class EvaluationError(ServiceError):
    def __init__(self, message, *, code='EVALUATION_INTEGRITY', status=409):
        super().__init__(message, status)
        self.code, self.retryable = code, False


def _need(condition, message, code='EVALUATION_INTEGRITY'):
    if not condition:
        raise EvaluationError(message, code=code)


def _decode(row):
    _need(row is not None, 'Evaluation resource not found', 'EVALUATION_NOT_FOUND')
    value = json.loads(row['payload'])
    _need(digest(value) == row['digest'], 'Evaluation payload identity differs')
    return value


def _now():
    return datetime.now(timezone.utc)


class ProviderEvaluationRuns:
    def __init__(self, store):
        self.store = store

    def _load(self, connection, identity):
        row = connection.execute('SELECT * FROM provider_evaluation_runs WHERE id=?', (identity,)).fetchone()
        value = _decode(row)
        plan = parse_execution_plan(value['plan']).model_dump()
        _need(value['id'] == identity and row['plan_digest'] == plan['digest'], 'Evaluation plan identity differs')
        from ..provider_evaluation_v2 import verify_plan
        verify_plan(plan, current=False)
        _need(row['created_at'] == value['created_at'] and row['deadline_at'] == value['deadline_at'], 'Evaluation deadline differs')
        qualification = json.loads(row['qualification']) if row['qualification'] else None
        if qualification is not None:
            _need(qualification['digest'] == digest({k:v for k,v in qualification.items() if k != 'digest'}), 'Qualification receipt differs')
        return {**value, 'qualification': qualification}

    def create(self, plan):
        from ..provider_evaluation_v2 import verify_plan
        plan = parse_execution_plan(plan).model_dump()
        verify_plan(plan, current=True)
        identity = 'provider_evaluation_' + plan['digest']
        with transaction(self.store.db_path) as connection:
            row = connection.execute('SELECT id FROM provider_evaluation_runs WHERE plan_digest=?', (plan['digest'],)).fetchone()
            if row:
                return self._load(connection, row['id'])
            # This entry point deliberately owns an isolated, single-plan workspace.
            _need(not connection.execute('SELECT 1 FROM provider_evaluation_runs').fetchone(), 'Evaluation workspace already has its immutable plan', 'EVALUATION_ALREADY_PLANNED')
            _need(not connection.execute('SELECT 1 FROM proposal_attempts').fetchone()
                  and not connection.execute('SELECT 1 FROM runs').fetchone(), 'Evaluation must begin before attempts or engine runs', 'EVALUATION_DIRTY_WORKSPACE')
            created = _now(); deadline = created + timedelta(seconds=plan['budget']['max_seconds'])
            value = {'id': identity, 'plan': plan, 'created_at': created.isoformat(), 'deadline_at': deadline.isoformat()}
            connection.execute('INSERT INTO provider_evaluation_runs VALUES(?,?,?,?,?,?,NULL)',
                (identity, plan['digest'], json_text(value), digest(value), value['created_at'], value['deadline_at']))
            return {**value, 'qualification': None}

    def get(self, identity):
        with closing(connect(self.store.db_path)) as connection:
            value = self._load(connection, identity)
            value['cases'] = [_decode(row) for row in connection.execute('SELECT * FROM provider_evaluation_cases WHERE evaluation_id=? ORDER BY case_id', (identity,))]
            value['attempts'] = [_decode(row) for row in connection.execute('SELECT * FROM provider_evaluation_attempts WHERE evaluation_id=? ORDER BY case_id', (identity,))]
            value['engines'] = [_decode(row) for row in connection.execute('SELECT * FROM provider_evaluation_engines WHERE evaluation_id=? ORDER BY slot', (identity,))]
            _need(len(value['attempts']) <= 3 and len(value['engines']) <= 2, 'Execution exceeded its fixed slot inventory')
            actual_attempts = {row[0] for row in connection.execute('SELECT id FROM proposal_attempts')}
            _need(actual_attempts == {p['attempt_id'] for p in value['attempts']}, 'Workspace attempts and charged evaluation slots differ')
            bound = {item['case_id']:item for item in value['cases']}
            for permit in value['attempts']:
                case = bound.get(permit['case_id'])
                _need(case is not None and permit['evaluation_id'] == identity
                      and permit['plan_digest'] == value['plan']['digest']
                      and permit['proposal_id'] == case['proposal_id']
                      and permit['request_digest'] == case['request_digest']
                      and permit['id'] == 'evaluation_permit_' + digest({'evaluation':identity,'case':permit['case_id']}),
                      'Charged slot differs from its original evaluation case')
                actual = connection.execute('SELECT proposal_id FROM proposal_attempts WHERE id=?', (permit['attempt_id'],)).fetchone()
                _need(actual[0] == permit['proposal_id'], 'Actual attempt belongs to another proposal')
            return value

    def bind_case(self, identity, case_id, request):
        from .provider_research_schema import ResearchProposalCreate
        from .research_sessions import ResearchSessions
        request = ResearchProposalCreate.model_validate(request).model_dump()
        with transaction(self.store.db_path) as connection:
            run = self._load(connection, identity)
            case = next((c for c in run['plan']['cases'] if c['id'] == case_id), None)
            _need(case is not None, 'Case is outside the frozen plan')
            value = {'evaluation_id': identity, 'case_id': case_id, 'session_id': request['session_id'],
                     'request': request, 'request_digest': digest(request), 'proposal_id': 'proposal_' + digest(request)}
            old = connection.execute('SELECT * FROM provider_evaluation_cases WHERE evaluation_id=? AND case_id=?', (identity, case_id)).fetchone()
            if old:
                _need(_decode(old) == value, 'Case cannot be rebound to another session or request', 'EVALUATION_CASE_CONFLICT')
                return value
            _need(_now() < datetime.fromisoformat(run['deadline_at']), 'Evaluation deadline elapsed', 'EVALUATION_EXPIRED')
            session = ResearchSessions(self.store)._load(connection, request['session_id'])
            _need(not session['reservations'], 'Bind the exact unused session before reserving its proposal')
            _need(request['expected_session_digest'] == session['row']['state_digest'], 'Initial session digest differs')
            _need(session['request']['config'] == {'provider_config_id': run['plan']['config']['config_id'],
                'provider_config_digest': run['plan']['native_identity']['config_digest'], 'contract_id': 'research_provider_v2'}, 'Session configuration differs')
            _need(session['request']['budget']['max_provider_dispatches'] == 1
                  and session['request']['budget']['max_experiments'] == int(case['allow_experiment']), 'Session permits exceed its case')
            _need(datetime.fromisoformat(session['row']['deadline_at']) <= datetime.fromisoformat(run['deadline_at']), 'Session deadline exceeds the global absolute deadline')
            ResearchEvaluationAuthority(self.store, identity)._request_matches(run, case, request, run['plan']['config'])
            connection.execute('INSERT INTO provider_evaluation_cases VALUES(?,?,?,?,?)',
                (identity, case_id, request['session_id'], json_text(value), digest(value)))
            return value

    def activate_qualification(self, identity):
        from .proposal_requests import ProposalRequests
        run = self.get(identity)
        binding = next((c for c in run['cases'] if c['case_id'] == 'qualification-generate'), None)
        _need(binding is not None, 'Qualification case has not been bound')
        proposals = ProposalRequests(self.store, configs={})
        proposal = proposals.get(binding['proposal_id'])
        _need(proposal['status'] == 'ready' and proposal['accepted_attempt_id'], 'Qualification requires an actual accepted generation', 'EVALUATION_UNQUALIFIED')
        attempt = next(a for a in proposal['attempts'] if a['id'] == proposal['accepted_attempt_id'])
        evidence = ResearchEvaluationAuthority(self.store, identity)._verify_saved_native(proposal, attempt)
        from .regression import oracle_support
        output = proposals._json_path(proposals._folder(proposal['id'])/'accepted/response.json')
        _need(output['outcome'] == 'draft' and len(output['draft']['candidates']) == 1
              and oracle_support(output['draft']['candidates'][0]['expression'])['formula'] == 'alpha101',
              'Qualification must implement the maintained Alpha101 formula', 'EVALUATION_UNQUALIFIED')
        value = {'evaluation_id': identity, 'proposal_id': proposal['id'], 'attempt_id': attempt['id'],
                 'accepted_response_digest': proposal['accepted_response_digest'], 'native_evidence': evidence,
                 'profile_digest': run['plan']['native_identity']['profile_digest']}
        value['digest'] = digest(value)
        with transaction(self.store.db_path) as connection:
            current = self._load(connection, identity)
            if current['qualification'] is not None:
                _need(current['qualification'] == value, 'Qualification evidence changed')
                return value
            _need(_now() < datetime.fromisoformat(current['deadline_at']), 'Qualification arrived after the deadline', 'EVALUATION_EXPIRED')
            connection.execute('UPDATE provider_evaluation_runs SET qualification=? WHERE id=?', (json_text(value), identity))
        return value

    def bind_engine(self, identity, slot, job_id):
        from .research_jobs import ResearchJobs
        _need(slot in {'baseline', 'provider'}, 'Unknown engine slot')
        jobs = ResearchJobs(self.store); job, body = jobs.get(job_id), jobs._body(job_id)
        with transaction(self.store.db_path) as connection:
            run = self._load(connection, identity)
            old = connection.execute('SELECT * FROM provider_evaluation_engines WHERE evaluation_id=? AND slot=?', (identity, slot)).fetchone()
            if old:
                value = _decode(old); _need(value['job_id'] == job_id, 'An engine slot cannot be rebound'); return value
            _need(_now() < datetime.fromisoformat(run['deadline_at']), 'Evaluation deadline elapsed', 'EVALUATION_EXPIRED')
            _need(job['run_id'] is None and job['state'] == 'created', 'Bind engine ownership before the first job action')
            if slot == 'baseline':
                expected = run['plan']['baseline']
                _need(job['research_id'] == expected['source']['research_id']
                      and job['base_revision_id'] == expected['source']['base_revision_id']
                      and digest(body['source']) == expected['source_digest'], 'Baseline job source differs')
                draft = {k: body['source']['revision']['task'][k] for k in ('evidence', 'hypotheses', 'candidates')}
                _need(body['request']['draft'] == draft, 'Baseline draft differs from the fixed maintained task')
            else:
                binding = connection.execute('SELECT * FROM provider_evaluation_cases WHERE evaluation_id=? AND case_id=?', (identity, 'alpha101-supported')).fetchone()
                binding = _decode(binding)
                experiment = connection.execute('SELECT * FROM research_session_experiments WHERE session_id=?', (binding['session_id'],)).fetchall()
                _need(len(experiment) == 1, 'Provider run requires its actual session-owned experiment')
                from .research_session_experiments import SessionExperiments
                actual = SessionExperiments(self.store).get(experiment[0]['id'])
                _need(actual['job']['id'] == job_id, 'Provider job belongs to another experiment')
            value = {'evaluation_id': identity, 'slot': slot, 'job_id': job_id,
                     'job_request_digest': self.store._fetch('research_jobs', job_id)['request_digest'], 'source_digest': digest(body['source']), 'run_id': None}
            connection.execute('INSERT INTO provider_evaluation_engines VALUES(?,?,?,?,?)', (identity, slot, job_id, json_text(value), digest(value)))
            return value

    def advance_engine(self, identity, slot, action):
        from .research_jobs import ResearchJobs
        run = self.get(identity); bound = next((e for e in run['engines'] if e['slot'] == slot), None)
        _need(bound is not None, 'Bind the original engine slot first')
        _need(action in {'validate', 'commit', 'submit', 'observe', 'complete'}, 'Unplanned engine action')
        jobs = ResearchJobs(self.store)
        job = jobs.get(bound['job_id'])
        _need(self.store._fetch('research_jobs', job['id'])['request_digest'] == bound['job_request_digest'], 'Original job changed')
        key = 'g4-engine-' + digest({'evaluation': identity, 'slot': slot, 'action': action})
        existing = next((s for s in job['steps'] if s['idempotency_key'] == key), None)
        # Completed original effects remain readable after the absolute cap;
        # they neither submit another run nor refresh any budget.
        if existing and existing['status'] in {'completed','failed','interrupted'}:
            step = existing
        elif _now() >= datetime.fromisoformat(run['deadline_at']):
            raise EvaluationError('Evaluation deadline elapsed', code='EVALUATION_EXPIRED')
        elif slot == 'provider':
            from .research_session_experiments import SessionExperiments
            binding = next(c for c in run['cases'] if c['case_id'] == 'alpha101-supported')
            records = self.store._read('SELECT id FROM research_session_experiments WHERE session_id=?', (binding['session_id'],))
            _need(len(records) == 1, 'Provider experiment association changed')
            actual = SessionExperiments(self.store).advance(records[0]['id'], action, key)
            _need(actual['job']['id'] == bound['job_id'], 'Provider job association changed')
            step = next(s for s in actual['job']['steps'] if s['idempotency_key'] == key)
        else:
            step = jobs.advance(bound['job_id'], action, key)
        job = jobs.get(bound['job_id'])
        if job['run_id']:
            actual = self.store._fetch('runs', job['run_id'])
            _need(actual['mode'] == 'normalized_fixed' and actual['revision_id'] == job['revision_id'], 'Bound engine run differs')
            with transaction(self.store.db_path) as connection:
                old = _decode(connection.execute('SELECT * FROM provider_evaluation_engines WHERE evaluation_id=? AND slot=?', (identity, slot)).fetchone())
                _need(old['run_id'] in {None, job['run_id']}, 'Engine slot already owns another run')
                old['run_id'] = job['run_id']
                connection.execute('UPDATE provider_evaluation_engines SET payload=?,digest=? WHERE evaluation_id=? AND slot=?', (json_text(old), digest(old), identity, slot))
        return {'step': step, 'job': job}

    def report(self, identity):
        from ..provider_evaluation_v2 import build_report
        return build_report(self.store, identity)


class ResearchEvaluationAuthority:
    """Explicit CLI-only authority; never loaded by an HTTP registry or setting."""
    def __init__(self, store, evaluation_id):
        self.store, self.evaluation_id = store, evaluation_id

    def _request_matches(self, run, case, request, config):
        config = config.model_dump() if hasattr(config, 'model_dump') else config
        _need(config == run['plan']['config'], 'Evaluation configuration differs', 'EVALUATION_CONFIG_MISMATCH')
        _need(request['schema_version'] == 2 and request['contract_id'] == 'research_provider_v2'
              and request['operation'] == 'generate_candidates' and request['target'] is None,
              'This plan authorizes generation only', 'EVALUATION_OPERATION_DENIED')
        _need(request['source'] == case['input']['source'] and request['expected_source_digest'] == case['input']['source_digest']
              and request['candidate_limit'] == 1, 'Case source or candidate policy differs', 'EVALUATION_REQUEST_MISMATCH')
        if run['plan']['schema_version'] == 2:
            from .provider_output_policy import policy_identity, policy_instruction
            policy_id = run['plan']['output_policy']['policy_id']
            _need(run['plan']['output_policy'] == policy_identity(policy_id)
                  and request['instruction'] == policy_instruction(policy_id),
                  'Request does not carry the exact frozen output policy', 'EVALUATION_POLICY_MISMATCH')
            _need(request['source']['kind'] == 'research_revision',
                  'Output policy supports maintained research revisions only', 'EVALUATION_POLICY_MISMATCH')
        budget = request['budget']
        _need(budget['max_provider_invocations'] == 1 and budget['max_seconds'] == 120
              and all(budget[k] is None for k in ('hard_token_limit','hard_model_call_limit','hard_cost_limit')),
              'Case dispatch budget differs', 'EVALUATION_BUDGET_MISMATCH')

    def verify_request(self, connection, request, config, *, phase):
        from ..provider_evaluation_v2 import verify_plan
        request = request.model_dump() if hasattr(request, 'model_dump') else request
        service = ProviderEvaluationRuns(self.store); run = service._load(connection, self.evaluation_id)
        verify_plan(run['plan'], current=True)
        _need(_now() < datetime.fromisoformat(run['deadline_at']), 'Global evaluation deadline elapsed', 'EVALUATION_EXPIRED')
        binding = _decode(connection.execute('SELECT * FROM provider_evaluation_cases WHERE evaluation_id=? AND session_id=?', (self.evaluation_id, request['session_id'])).fetchone())
        _need(binding['request'] == request, 'Request differs from its predeclared case binding', 'EVALUATION_REQUEST_MISMATCH')
        case = next(c for c in run['plan']['cases'] if c['id'] == binding['case_id'])
        self._request_matches(run, case, request, config)
        if case['role'] != 'qualification':
            _need(run['qualification'] is not None, 'The exact generation profile has not qualified', 'EVALUATION_UNQUALIFIED')
            self._qualification_current(run)
        _need(_now() < datetime.fromisoformat(run['deadline_at']), 'Evaluation deadline elapsed during verification', 'EVALUATION_EXPIRED')
        return {'run': run, 'binding': binding, 'case': case}

    def _qualification_current(self, run):
        from .proposal_requests import ProposalRequests
        qualification = run['qualification']; proposal = ProposalRequests(self.store, configs={}).get(qualification['proposal_id'])
        _need(proposal['status'] == 'ready' and proposal['accepted_response_digest'] == qualification['accepted_response_digest'], 'Accepted qualification changed', 'EVALUATION_UNQUALIFIED')
        attempt = next(a for a in proposal['attempts'] if a['id'] == qualification['attempt_id'])
        _need(self._verify_saved_native(proposal, attempt) == qualification['native_evidence'], 'Qualification native evidence changed')

    def reserve_attempt(self, connection, proposal, attempt_id):
        checked = self.verify_request(connection, proposal['request'], proposal['config'], phase='claim')
        binding = checked['binding']; permit_id = 'evaluation_permit_' + digest({'evaluation': self.evaluation_id, 'case': binding['case_id']})
        old = connection.execute('SELECT * FROM provider_evaluation_attempts WHERE id=?', (permit_id,)).fetchone()
        if old:
            value = _decode(old)
            _need(value['proposal_id'] == proposal['id'] and value['attempt_id'] == attempt_id, 'Spent slot cannot authorize a replacement attempt', 'EVALUATION_ATTEMPT_SPENT')
            return value
        count = connection.execute('SELECT COUNT(*) FROM provider_evaluation_attempts WHERE evaluation_id=?', (self.evaluation_id,)).fetchone()[0]
        _need(count < checked['run']['plan']['budget']['max_real_provider_attempts'], 'Global attempt budget exhausted', 'EVALUATION_BUDGET_EXCEEDED')
        _need(proposal['id'] == binding['proposal_id'], 'Proposal belongs to another request')
        value = {'id': permit_id, 'evaluation_id': self.evaluation_id, 'plan_digest': checked['run']['plan']['digest'],
            'case_id': binding['case_id'], 'proposal_id': proposal['id'], 'attempt_id': attempt_id,
            'request_digest': binding['request_digest'], 'config_digest': checked['run']['plan']['native_identity']['config_digest'],
            'profile_digest': checked['run']['plan']['native_identity']['profile_digest'],
            'prepared_digest': None, 'fencing_token': None, 'dispatch_intent': None, 'reserved_at': _now().isoformat()}
        _need(_now() < datetime.fromisoformat(checked['run']['deadline_at']), 'Evaluation deadline elapsed before charge', 'EVALUATION_EXPIRED')
        connection.execute('INSERT INTO provider_evaluation_attempts VALUES(?,?,?,?,?,?,?)', (permit_id,self.evaluation_id,binding['case_id'],proposal['id'],attempt_id,json_text(value),digest(value)))
        return value

    def authorize_prepared(self, proposal, attempt, prepared_digest):
        with transaction(self.store.db_path) as connection:
            checked = self.verify_request(connection, proposal['request'], proposal['config'], phase='claim')
            row = connection.execute('SELECT * FROM provider_evaluation_attempts WHERE attempt_id=? AND evaluation_id=?', (attempt['id'], self.evaluation_id)).fetchone()
            value = _decode(row)
            _need(value['proposal_id'] == proposal['id'] and attempt['prepared_digest'] == prepared_digest, 'Prepared ownership differs')
            _need(value['prepared_digest'] in {None, prepared_digest} and value['fencing_token'] in {None, attempt['fencing_token']}, 'Prepared identity cannot change')
            if checked['run']['plan']['schema_version'] == 2:
                from .proposal_requests import ProposalRequests
                folder = ProposalRequests(self.store, configs={})._folder(proposal['id'])/'attempts'/attempt['id']
                prepared = ProposalRequests(self.store, configs={})._json_path(folder/'prepared.json')
                _need(digest(prepared) == prepared_digest, 'Prepared output policy anchor differs', 'EVALUATION_POLICY_MISMATCH')
                self._verify_policy_prepared(proposal, attempt, prepared)
            value.update(prepared_digest=prepared_digest, fencing_token=attempt['fencing_token'])
            connection.execute('UPDATE provider_evaluation_attempts SET payload=?,digest=? WHERE id=?', (json_text(value),digest(value),value['id']))
            return value

    def _verify_policy_prepared(self, proposal, attempt, prepared):
        """Hash equality alone cannot prove the prompt came from its bound request."""
        from .provider_contract_registry import request_handler
        from .provider_output_policy import validate_policy_context
        handler = request_handler(proposal['request'])
        request = handler.parse_request(proposal['request'])
        validate_policy_context(proposal['context'])
        expected = {'proposal_id':proposal['id'], 'attempt_id':attempt['id'],
            'context_digest':proposal['context_digest'],
            'input_text':handler.build_prompt(request, proposal['context'], proposal['id'], attempt['id']),
            'output_schema':handler.output_schema(proposal['context']), 'provider_config':proposal['config'],
            'budget':proposal['request']['budget']}
        _need(prepared == expected, 'Actual prepared input differs from its frozen policy request',
              'EVALUATION_POLICY_MISMATCH')

    def _summary(self, value):
        return {**{key: value[key] for key in ('evaluation_id','plan_digest','case_id','proposal_id','attempt_id','request_digest','config_digest','profile_digest','prepared_digest')},
                'permit_id': value['id'], 'contract_id': 'research_provider_v2', 'operation': 'generate_candidates',
                'dispatch_intent_digest': digest(value['dispatch_intent']) if value['dispatch_intent'] is not None else None}

    def verify_native(self, request, *, phase):
        from .proposal_requests import ProposalRequests
        from .provider_contract_registry import request_handler
        _need(phase in {'entry','dispatch'}, 'Unknown native authorization phase')
        proposals = ProposalRequests(self.store, configs={}, research_authority=self)
        with transaction(self.store.db_path) as connection:
            proposal = proposals._load(connection, request.proposal_id)
            checked = self.verify_request(connection, proposal['request'], proposal['config'], phase='claim')
            attempt = next((a for a in proposal['attempts'] if a['id'] == request.attempt_id), None)
            _need(attempt is not None and proposal['status'] == 'running' and attempt['status'] == 'running', 'Native attempt is not active')
            from .research_session_execution import SessionExecution
            SessionExecution(self.store, proposals).authorize_proposal_dispatch(connection, proposal)
            row = connection.execute('SELECT * FROM provider_evaluation_attempts WHERE evaluation_id=? AND attempt_id=?', (self.evaluation_id, request.attempt_id)).fetchone()
            value = _decode(row)
            expected = {'proposal_id':request.proposal_id,'attempt_id':request.attempt_id,'context_digest':request.context_digest,
                'input_text':request.input_text,'output_schema':request.output_schema,'provider_config':request.config.model_dump(),'budget':request.budget.model_dump()}
            _need(value['prepared_digest'] == digest(expected) == attempt['prepared_digest'] and value['fencing_token'] == attempt['fencing_token'], 'Native request differs from the actual prepared attempt')
            if checked['run']['plan']['schema_version'] == 2:
                self._verify_policy_prepared(proposal, attempt, expected)
            handler = request_handler(proposal['request'])
            _need(request.output_schema == handler.output_schema(proposal['context']), 'Native output schema differs')
            if phase == 'dispatch':
                _need(value['dispatch_intent'] is None, 'Original native dispatch permit is already consumed', 'EVALUATION_DISPATCH_SPENT')
                value['dispatch_intent'] = {'permit_id':value['id'],'attempt_id':request.attempt_id,'prepared_digest':value['prepared_digest'],'created_at':_now().isoformat()}
                connection.execute('UPDATE provider_evaluation_attempts SET payload=?,digest=? WHERE id=?', (json_text(value),digest(value),value['id']))
            return self._summary(value)

    def _verify_saved_native(self, proposal, attempt, result=None):
        from .provider_research_qualification import verify_native_attempt
        with closing(connect(self.store.db_path)) as connection:
            value = _decode(connection.execute('SELECT * FROM provider_evaluation_attempts WHERE evaluation_id=? AND attempt_id=?', (self.evaluation_id,attempt['id'])).fetchone())
            _need(value['proposal_id'] == proposal['id'] and value['prepared_digest'] == attempt['prepared_digest']
                  and value['dispatch_intent'] is not None, 'Accepted result lacks its consumed exact native permit')
        evidence = verify_native_attempt(self.store, proposal, attempt, result=result)
        from .provider_supervision_journal import read_metadata
        from .proposal_requests import ProposalRequests
        folder = ProposalRequests(self.store, configs={})._folder(proposal['id'])/'attempts'/attempt['id']/'transport'
        native = read_metadata(folder/'native-evidence.json', 512*1024)
        intent = read_metadata(folder/'dispatch-intent.json')
        expected = self._summary(value)
        _need(native.get('research_authorization_dispatch') == expected
              and intent.get('research_authorization') == expected
              and native.get('research_authorization_entry') == {**expected, 'dispatch_intent_digest': None},
              'Native evidence differs from the actual consumed global permit')
        return evidence

    def verify_result(self, proposal, attempt, *, response, receipt, error):
        evidence = self._verify_saved_native(proposal, attempt, {'response':response,'receipt':receipt,'error':error})
        with closing(connect(self.store.db_path)) as connection:
            run = ProviderEvaluationRuns(self.store)._load(connection, self.evaluation_id)
        if run['plan']['schema_version'] == 2:
            from .provider_output_policy import validate_output_policy
            verdict = validate_output_policy(proposal['context'], response)
            _need(verdict['valid'], 'Provider output does not satisfy the frozen machine output policy',
                  'EVALUATION_OUTPUT_POLICY_INVALID')
        return evidence
