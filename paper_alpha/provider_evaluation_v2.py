"""Freeze G4 inputs and report actual queue evidence; never call a provider here."""
from copy import deepcopy
import hashlib
import os
import stat
import platform
from pathlib import Path

import pydantic

from .storage import digest, read_json
from .provider_evaluation_v2_schema import parse_execution_plan, EvaluationInput, EvaluationBudget
from .server.provider_schema import ProviderConfig

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / 'evaluation_suites/provider_dev_v2/protocol.json'
POLICY_SUITE = ROOT / 'evaluation_suites/provider_dev_v3/protocol.json'
CASE_IDS = ('qualification-generate', 'alpha101-supported', 'alpha005-missing-vwap')


def _need(condition, message):
    from .server.provider_evaluation_runs import EvaluationError
    if not condition:
        raise EvaluationError(message)


def _suite(suite_id):
    _need(suite_id in {'provider_dev_v2', 'provider_dev_v3'}, 'Unsupported evaluation suite')
    return SUITE if suite_id == 'provider_dev_v2' else POLICY_SUITE


MAX_IMPLEMENTATION_FILES = 2000
MAX_IMPLEMENTATION_DIRECTORIES = 4000
MAX_IMPLEMENTATION_DEPTH = 32
MAX_IMPLEMENTATION_ENTRIES = 8000
MAX_IMPLEMENTATION_FILE_BYTES = 2 * 1024 * 1024
MAX_IMPLEMENTATION_TOTAL_BYTES = 128 * 1024 * 1024


def _implementation_directory(parent, parts):
    """Open a directory chain without following any substituted locator."""
    descriptor = os.dup(parent)
    try:
        for part in parts:
            _need(part not in {'', '.', '..'} and '/' not in part,
                  'Unsafe evaluation implementation locator')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _implementation_root(root):
    root = Path(root).absolute()
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        return _implementation_directory(descriptor, root.parts[1:])
    finally:
        os.close(descriptor)


def _implementation_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _implementation_paths(root_descriptor, suite_id):
    """Closed product inputs, with no development orchestrators or tools.

    Old stored plans retain their original inventories. The current product
    recipe can differ after relocation; such drift never renews an authority.
    Directory enumeration and all file reads have separate finite bounds.
    """
    _need(type(suite_id) is str and suite_id in {'provider_dev_v2', 'provider_dev_v3'},
          'Unsupported evaluation suite')
    pending = [('paper_alpha', True), ('contracts', False),
               ('evaluation_suites/' + suite_id, False)]
    paths = []
    directories = 0
    entries_seen = 0
    while pending:
        relative, python_only = pending.pop()
        parts = Path(relative).parts
        _need(len(parts) <= MAX_IMPLEMENTATION_DEPTH,
              'Evaluation implementation depth exceeded')
        descriptor = _implementation_directory(root_descriptor, parts)
        try:
            directories += 1
            _need(directories <= MAX_IMPLEMENTATION_DIRECTORIES,
                  'Evaluation implementation directory bound exceeded')
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    entries_seen += 1
                    _need(entries_seen <= MAX_IMPLEMENTATION_ENTRIES,
                          'Evaluation implementation entry bound exceeded')
                    # Cache and OS metadata are outside the product inventory.
                    if entry.name.startswith('.') or entry.name == '__pycache__':
                        continue
                    info = entry.stat(follow_symlinks=False)
                    _need(not stat.S_ISLNK(info.st_mode),
                          'Symlink evaluation implementation input')
                    name = relative + '/' + entry.name
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((name, python_only))
                        _need(directories + len(pending) <= MAX_IMPLEMENTATION_DIRECTORIES,
                              'Evaluation implementation directory bound exceeded')
                    else:
                        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                              'Nonregular evaluation implementation input')
                        if not python_only or name.endswith('.py'):
                            _need(info.st_size <= MAX_IMPLEMENTATION_FILE_BYTES,
                                  'Evaluation implementation file bound exceeded')
                            paths.append(name)
                            _need(len(paths) <= MAX_IMPLEMENTATION_FILES,
                                  'Evaluation implementation file count exceeded')
        finally:
            os.close(descriptor)
    _need(any(name.startswith('paper_alpha/') for name in paths)
          and any(name.startswith('contracts/') for name in paths)
          and 'evaluation_suites/' + suite_id + '/protocol.json' in paths,
          'Required evaluation implementation input missing')
    return tuple(sorted(paths))


def _implementation_stat(root_descriptor, relative):
    parts = Path(relative).parts
    parent = _implementation_directory(root_descriptor, parts[:-1])
    try:
        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
              'Nonregular evaluation implementation input')
        return _implementation_signature(info)
    finally:
        os.close(parent)


def _implementation_file(root_descriptor, relative, remaining, expected_signature):
    parts = Path(relative).parts
    parent = _implementation_directory(root_descriptor, parts[:-1])
    descriptor = None
    reopened = None
    try:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=parent)
        before = os.fstat(descriptor)
        _need(_implementation_signature(before) == expected_signature,
              'Evaluation implementation input changed')
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
              'Nonregular evaluation implementation input')
        _need(before.st_size <= min(MAX_IMPLEMENTATION_FILE_BYTES, remaining),
              'Evaluation implementation byte bound exceeded')
        hasher = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, remaining - size + 1))
            if not chunk:
                break
            size += len(chunk)
            _need(size <= min(MAX_IMPLEMENTATION_FILE_BYTES, remaining),
                  'Evaluation implementation byte bound exceeded')
            hasher.update(chunk)
        after = os.fstat(descriptor)
        reopened = _implementation_directory(root_descriptor, parts[:-1])
        located = os.stat(parts[-1], dir_fd=reopened, follow_symlinks=False)
        _need(_implementation_signature(before) == _implementation_signature(after)
              == _implementation_signature(located) and size == after.st_size,
              'Evaluation implementation input changed')
        return hasher.hexdigest(), size
    finally:
        for candidate in (reopened, descriptor, parent):
            if candidate is not None:
                os.close(candidate)


def implementation(suite_id='provider_dev_v2'):
    """Fingerprint bounded product inputs; source-only drivers are excluded.

    The return mapping and every saved plan stay on their original contract.
    A historical full-source inventory can compare false in this runtime,
    without crashing because an intentionally omitted driver is unavailable.
    """
    descriptor = reopened = None
    try:
        descriptor = _implementation_root(ROOT)
        paths = _implementation_paths(descriptor, suite_id)
        signatures = {name: _implementation_stat(descriptor, name) for name in paths}
        files = {}
        used = 0
        for name in paths:
            fingerprint, size = _implementation_file(
                descriptor, name, MAX_IMPLEMENTATION_TOTAL_BYTES - used, signatures[name])
            files[name] = fingerprint
            used += size
        _need(paths == _implementation_paths(descriptor, suite_id),
              'Evaluation implementation inventory changed')
        _need(signatures == {name: _implementation_stat(descriptor, name) for name in paths},
              'Evaluation implementation input changed')
        reopened = _implementation_root(ROOT)
        _need((os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
              == (os.fstat(reopened).st_dev, os.fstat(reopened).st_ino),
              'Evaluation implementation root changed')
        return files
    except OSError:
        _need(False, 'Evaluation implementation input unavailable')
    finally:
        for candidate in (reopened, descriptor):
            if candidate is not None:
                os.close(candidate)


def environment():
    return {'python':platform.python_version(),'pydantic':pydantic.__version__}


def input_identity(store, descriptor, *, hypothesis_id):
    from .server.provider_research_context import resolve_research_source
    source = resolve_research_source(store, descriptor)
    task = source['revision']['task']
    _need(len(task['hypotheses']) == len(task['evidence']) == len(task['candidates']) == 1,
          'Each evaluation source must contain one maintained hypothesis/evidence/candidate')
    _need(task['hypotheses'][0]['id'] == hypothesis_id
          and task['hypotheses'][0]['attribution'] == 'paper_original'
          and task['candidates'][0]['hypothesis_id'] == hypothesis_id,
          'Evaluation task differs from its predeclared Alpha101 hypothesis')
    return EvaluationInput.model_validate({'source':descriptor,'source_digest':digest(source),
        'task_digest':digest(task), **{key:source[key] for key in ('paper_sha256','dataset_sha256','data_file_sha256','metadata_file_sha256')},
        'evidence_ids':[item['id'] for item in task['evidence']],
        'hypothesis_ids':[item['id'] for item in task['hypotheses']]}).model_dump()


def build_plan(store, config, case_sources, *, baseline_source, output_policy_id=None):
    """No model, queue or calculation calls; freeze all three slots in advance."""
    from .server.provider_native import _identity
    config = ProviderConfig.model_validate(config)
    _need(set(case_sources) == set(CASE_IDS), 'All and only the three fixed cases are required')
    native, profile = _identity(config, contract_id='research_provider_v2')
    _need(profile['static_checks']['passed'], 'Pinned v2 native profile failed static checks')
    suite_id = 'provider_dev_v2' if output_policy_id is None else 'provider_dev_v3'
    suite = _suite(suite_id)
    protocol = read_json(suite)
    policy = None
    if output_policy_id is not None:
        from .server.provider_output_policy import policy_identity
        policy = policy_identity(output_policy_id)
        _need(protocol['output_policy_id'] == policy['policy_id'], 'Suite output policy differs')
    cases = []
    for case in protocol['cases']:
        cases.append({'id':case['id'],'role':case['role'],
            'input':input_identity(store,case_sources[case['id']],hypothesis_id=case['hypothesis_id']),
            'operation':'generate_candidates','max_attempts':1,'candidate_limit':1,
            'allow_experiment':case['role']=='positive'})
    baseline = input_identity(store,baseline_source,hypothesis_id='h-alpha101')
    for name in ('task_digest','paper_sha256','dataset_sha256','data_file_sha256','metadata_file_sha256'):
        _need(baseline[name] == cases[1]['input'][name] == cases[0]['input'][name], 'Baseline, qualification and positive-case scientific inputs must match')
    _need(len({c['input']['source']['research_id'] for c in cases} | {baseline['source']['research_id']}) == 4,
          'Each planned case and baseline must use its own research to preserve frozen bases')
    body = {'schema_version':1 if policy is None else 2,'kind':'provider_development_execution_plan','suite_id':suite_id,
        'contract_id':'research_provider_v2','operation':'generate_candidates','mode':'provider_one_shot',
        'repetitions':1,'config':config.model_dump(),'native_identity':native,
        'suite_digest':hashlib.sha256(suite.read_bytes()).hexdigest(),
        'implementation':implementation(suite_id),'environment':environment(),'cases':cases,
        'baseline':baseline,'budget':EvaluationBudget().model_dump(),
        'semantic_fidelity':'unverified','final_test_execution':'locked'}
    if policy is not None:
        body['output_policy'] = policy
    fingerprint = digest(body)
    return parse_execution_plan({**body,'id':'provider_eval_plan_'+fingerprint,'digest':fingerprint}).model_dump()


def verify_plan(plan, *, current=False):
    plan = parse_execution_plan(plan).model_dump()
    body = {k:v for k,v in plan.items() if k not in {'id','digest'}}
    _need(plan['digest'] == digest(body) and plan['id'] == 'provider_eval_plan_'+plan['digest'], 'Plan digest differs')
    if current:
        from .server.provider_native import _identity
        _need(plan['implementation'] == implementation(plan['suite_id']) and plan['environment'] == environment(), 'Evaluation source or environment changed')
        _need(plan['suite_digest'] == hashlib.sha256(_suite(plan['suite_id']).read_bytes()).hexdigest(), 'Suite changed')
        if plan['schema_version'] == 2:
            from .server.provider_output_policy import policy_identity
            _need(plan['output_policy'] == policy_identity(plan['output_policy']['policy_id']), 'Output policy changed')
        actual, profile = _identity(ProviderConfig.model_validate(plan['config']), contract_id='research_provider_v2')
        _need(profile['static_checks']['passed'] and plan['native_identity'] == actual, 'Native profile changed')
    return plan


def _ratio(numerator, denominator):
    return {'numerator':numerator,'denominator':denominator,'rate':numerator/denominator if denominator else None}


def _engine_observation(store, bound):
    from .server.research_jobs import ResearchJobs
    from .server.regression import verify_candidate, oracle_support
    job = ResearchJobs(store).get(bound['job_id'])
    _need(store._fetch('research_jobs',job['id'])['request_digest'] == bound['job_request_digest'], 'Engine ownership changed')
    _need(job['run_id'] == bound['run_id'], 'Engine run identity differs')
    result = {'slot':bound['slot'],'job_id':job['id'],'state':job['state'],'run_id':job['run_id'],
              'verification':None,'oracle':None,'metrics':None,'task_digest':None}
    if job['state'] != 'completed':
        return result
    actual = store.get_run(job['run_id'])
    _need(actual['status']=='completed' and (actual.get('verification') or {}).get('verified') is True,
          'Completed engine result failed artifact verification')
    row = store._fetch('runs',job['run_id'])
    output = store.root/'runs'/row['id']/'attempts'/row['attempt_id']/'output'
    task = read_json(output/'inputs/task.json')
    _need(len(task['candidates']) == 1, 'Engine result is outside the single-candidate case')
    oracle = verify_candidate(output,task['candidates'][0]['id'],task)
    # The public Store view intentionally strips daily series. Read the original
    # artifact only after the existing manifest verifier has accepted this run.
    recorded_state = read_json(output/'state.json')
    computed = recorded_state['candidates'][0].get('result')
    result.update(verification=actual['verification'],oracle=oracle,
                  task_digest=digest(task),candidate=task['candidates'][0],
                  metrics=(computed or {}).get('metrics'),
                  computed_digest=digest(computed) if computed is not None else None,
                  comparison_fields={name:digest(computed[name]) if isinstance(computed,dict) and name in computed else None
                                     for name in ('split','config','execution','metrics','daily')},
                  formula=oracle_support(task['candidates'][0]['expression'])['formula'],
                  scientific_input_digest=digest({name:task[name] for name in ('evidence','hypotheses','evaluation')}),
                  input_sha256={name:hashlib.sha256((output/'inputs'/name).read_bytes()).hexdigest()
                                for name in ('paper.json','paper.pdf','market.csv','metadata.json')})
    return result


def build_report(store, evaluation_id):
    """All observations come from original records; failed/unrun cases stay visible."""
    from .server.provider_evaluation_runs import ProviderEvaluationRuns, ResearchEvaluationAuthority
    from .server.proposal_requests import ProposalRequests
    run = ProviderEvaluationRuns(store).get(evaluation_id)
    actual_run_ids = {row['id'] for row in store._read('SELECT id FROM runs')}
    bound_run_ids = {item['run_id'] for item in run['engines'] if item['run_id'] is not None}
    _need(actual_run_ids == bound_run_ids, 'Workspace engine runs and fixed evaluation slots differ; reconcile original effects first')
    for bound in run['engines']:
        if bound['run_id'] is not None:
            attempts = store._read('SELECT id FROM attempts WHERE run_id=?', (bound['run_id'],))
            _need(len(attempts) <= 1, 'A fixed engine slot cannot conceal a retried computation')
    requests = ProposalRequests(store,configs={})
    authority = ResearchEvaluationAuthority(store,evaluation_id)
    policy_enabled = run['plan']['schema_version'] == 2
    engines = [_engine_observation(store,bound) for bound in run['engines']]
    provider_engine = next((e for e in engines if e['slot']=='provider'),None)
    baseline = next((e for e in engines if e['slot']=='baseline'),None)
    records, receipts, observed_attempts = [], [], []
    for case in run['plan']['cases']:
        binding = next((c for c in run['cases'] if c['case_id']==case['id']),None)
        record = {'case_id':case['id'],'role':case['role'],'repetition':1,'outcome':'not_run',
            'proposal_id':None,'attempt_id':None,'accepted_response_digest':None,
            'schema_accepted':False,'domain_accepted':False,'correct_abstention':False,
            'passed':False,'error':None,'native_evidence':None,'engine':None}
        if policy_enabled:
            record.update(policy_verdict=None, safe_abstention=None,
                          machine_policy_compliance=None, semantic_fidelity='unverified')
        if binding and store._read('SELECT 1 FROM proposal_requests WHERE id=?',(binding['proposal_id'],)):
            proposal = requests.get(binding['proposal_id'])
            _need(proposal['request'] == binding['request'], 'Reported proposal differs from its case')
            record.update(proposal_id=proposal['id'],outcome=proposal['status'],error=proposal['error'],
                          accepted_response_digest=proposal['accepted_response_digest'])
            _need(len(proposal['attempts']) <= 1, 'Case has extra attempts')
            if proposal['attempts']:
                attempt = proposal['attempts'][0]
                permit = next((p for p in run['attempts'] if p['attempt_id']==attempt['id']),None)
                _need(permit is not None and permit['proposal_id']==proposal['id'], 'Attempt lacks its original global charge')
                record['attempt_id']=attempt['id']; receipts.append(attempt['receipt']); observed_attempts.append(attempt)
                record['schema_accepted']=attempt['response_digest'] is not None
                record['domain_accepted']=(attempt['validation'] or {}).get('valid') is True
                policy_rejected = (policy_enabled and proposal['status'] == 'rejected'
                    and (proposal['error'] or {}).get('code') == 'EVALUATION_OUTPUT_POLICY_INVALID')
                if proposal['status'] in {'ready','abstained'} or policy_rejected:
                    output = requests._json_path(requests._folder(proposal['id'])/'attempts'/attempt['id']/'parsed.json')
                    # A successful native transport may be rejected by the host's
                    # stricter output policy. Preserve both facts separately.
                    transport_result = ({'response':output,'receipt':attempt['receipt'],'error':None}
                                        if policy_rejected else None)
                    record['native_evidence']=authority._verify_saved_native(proposal,attempt,transport_result)
                    if policy_enabled:
                        from .server.provider_output_policy import validate_output_policy
                        verdict = validate_output_policy(proposal['context'], output)
                        no_experiment = not store._read('SELECT 1 FROM research_session_experiments WHERE session_id=?', (binding['session_id'],))
                        record.update(policy_verdict=verdict,
                            safe_abstention=bool(verdict['capability_supported_stop'] and no_experiment),
                            machine_policy_compliance=verdict['valid'])
                    if case['role']=='negative' and output['outcome']=='abstain':
                        abstention=output['abstention']; context=proposal['context']
                        fields={f['name'] for f in context['capabilities']['fields']}
                        missing=set(context['hypotheses'][0]['required_fields'])-fields
                        if policy_enabled:
                            record['correct_abstention'] = bool(record['safe_abstention'] and record['machine_policy_compliance'])
                        else:
                            record['correct_abstention']=(missing=={'vwap'} and abstention['reason_code']=='FIELD_UNAVAILABLE'
                                and set(abstention['missing_requirements'])=={'field:vwap'}
                                and set(abstention['evidence_ids'])==set(case['input']['evidence_ids'])
                                and not store._read('SELECT 1 FROM research_session_experiments WHERE session_id=?',(binding['session_id'],)))
                        record['passed']=record['correct_abstention']
                    elif case['role']=='qualification':
                        record['passed']=proposal['status']=='ready' and run['qualification'] is not None
                    elif case['role']=='positive':
                        record['engine']=provider_engine
                        record['passed']=bool(provider_engine and provider_engine['state']=='completed'
                            and (provider_engine['oracle'] or {}).get('outcome')=='passed'
                            and provider_engine.get('formula')=='alpha101')
        records.append(record)
    quality=[r for r in records if r['role']!='qualification']
    positive=next(r for r in records if r['role']=='positive');negative=next(r for r in records if r['role']=='negative')
    known_started=sum(bool(r and r['invocation_started']) for r in receipts)
    def usage_total(key):
        if any(r is None or r['usage'].get(key) is None for r in receipts): return None
        return sum(r['usage'][key] for r in receipts) if receipts else None
    comparison = {'same_scientific_input':False,'same_input_bytes':False,'same_alpha101_formula':False,
                  'equal_computed_fields':{k:False for k in ('split','config','execution','metrics','daily')}}
    if provider_engine and baseline and provider_engine['state']==baseline['state']=='completed':
        comparison.update(same_scientific_input=provider_engine['scientific_input_digest']==baseline['scientific_input_digest'],
                          same_input_bytes=provider_engine['input_sha256']==baseline['input_sha256'],
                          same_alpha101_formula=provider_engine['formula']==baseline['formula']=='alpha101',
                          equal_computed_fields={k:provider_engine['comparison_fields'][k] is not None and
                              provider_engine['comparison_fields'][k]==baseline['comparison_fields'][k]
                              for k in comparison['equal_computed_fields']})
    numerical_pass=bool(provider_engine and baseline and provider_engine['oracle'] and baseline['oracle']
                        and provider_engine['oracle']['outcome']==baseline['oracle']['outcome']=='passed'
                        and comparison['same_scientific_input'] and comparison['same_input_bytes']
                        and comparison['same_alpha101_formula'] and all(comparison['equal_computed_fields'].values()))
    report = {'schema_version':2 if policy_enabled else 1,'kind':'provider_development_execution_report','evaluation_id':evaluation_id,
        'plan_id':run['plan']['id'],'plan_digest':run['plan']['digest'],'measurement_scope':'actual_queue_development',
        'records':records,'engines':engines,'baseline_comparison':comparison,'qualification':run['qualification'],
        'budgets':run['plan']['budget'],'actual_attempts_reserved':len(run['attempts']),
        'confirmed_native_invocations_started':known_started,
        'unknown_invocation_outcomes':sum(a['status']=='interrupted' or a['receipt'] is None
                                         or a['receipt']['process_stopped'] is not True for a in observed_attempts),
        'actual_engine_runs':sum(bool(e['run_id']) for e in engines),
        'metrics':{'task_handling':_ratio(sum(r['passed'] for r in quality),2),
            'positive_completion':_ratio(int(positive['passed']),1),
            'correct_abstention':_ratio(int(negative['passed']),1),
            'schema_acceptance_planned':_ratio(sum(r['schema_accepted'] for r in quality),2),
            'domain_acceptance_planned':_ratio(sum(r['domain_accepted'] for r in quality),2),
            'numerical_agreement':_ratio(int(numerical_pass),1)},
        'backend_model_calls':usage_total('model_calls'),'input_tokens':usage_total('input_tokens'),
        'output_tokens':usage_total('output_tokens'),'supplier_cost':usage_total('cost'),
        'model_reproducibility':None,'business_compute_reproducibility':None,
        'model_quality':None,'human_quality':None,'human_time_saved':None,'semantic_fidelity':'unverified',
        'final_test_execution':'locked','implementation_matches_current':run['plan']['implementation']==implementation(run['plan']['suite_id']),
        'limitations':['One repetition over previously used synthetic development material cannot establish model stability or profitability.',
            'Correct abstention is a mechanical required-field/capability/evidence check; semantic quality remains unreviewed.',
            'CLI attempt slots are not hard limits on supplier model requests, token usage or currency cost.']}
    if policy_enabled:
        report['output_policy'] = run['plan']['output_policy']
        report['metrics'].update(
            safe_abstention=_ratio(int(negative['safe_abstention'] is True),1),
            machine_policy_compliance_planned=_ratio(sum(r['machine_policy_compliance'] is True for r in quality),2))
        report['limitations'].append('Safe abstention observes a capability-supported stop without an experiment; it does not interpret free-text requirements or establish semantic fidelity.')
    return report


def report_markdown(report):
    lines=['# G4 固定开发评测','',f"计划：`{report['plan_id']}`",'',
        '| 指标 | 通过 | 固定分母 | 比例 |','|---|---:|---:|---:|']
    for name,value in report['metrics'].items():
        lines.append(f"| {name} | {value['numerator']} | {value['denominator']} | {value['rate']} |")
    lines += ['',f"已预留 CLI attempts：{report['actual_attempts_reserved']}；确认启动：{report['confirmed_native_invocations_started']}；实际 engine runs：{report['actual_engine_runs']}。",
              '','一次重复不提供复现率；科学忠实度、人工质量和节省时间未验证。','']
    lines += [f"- {r['case_id']}: {r['outcome']}; passed={r['passed']}" for r in report['records']]
    if report['schema_version'] == 2:
        lines += ['', '安全停止与机器格式合规分别计数；模型语义忠实度仍未验证。', '']
        lines += [f"- {r['case_id']}: safe_abstention={r['safe_abstention']}; machine_policy_compliance={r['machine_policy_compliance']}"
                  for r in report['records']]
    return '\n'.join(lines)+'\n'
