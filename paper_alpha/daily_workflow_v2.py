"""Registered development workflow; closed jobs, prefix-only tools, retained attempts."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,os,sqlite3,sys,time
from pathlib import Path
from types import SimpleNamespace
from .contracts import require,Evidence
from .daily_adapter import precheck_development,_open_regular,_stable
from .daily_contract import MAX_RAW_BYTES
from .daily_task_v2 import parse_daily_task
from .evaluation import evaluate_development
from .evidence import verify_paper,match_evidence
from .expressions import compute_expression
from .storage import atomic_json,digest,json_text
from .server.daily_research_v2 import assert_daily_task,inspect_daily_task

RUN_VERSION='registered-daily-workflow-v2'
CODE_FILES=('paper_alpha/daily_workflow_v2.py','paper_alpha/daily_task_v2.py','paper_alpha/server/daily_research_v2.py','paper_alpha/server/daily_context_v2.py','paper_alpha/server/daily_source_qualification_v2.py','paper_alpha/worker.py','paper_alpha/daily_adapter.py','paper_alpha/daily_contract.py','paper_alpha/evaluation.py','paper_alpha/expressions.py','paper_alpha/contracts.py','paper_alpha/storage.py','paper_alpha/vendor/operators.py','paper_alpha/server/provider_catalog.py','paper_alpha/server/daily_snapshots.py','paper_alpha/server/daily_snapshot_schema.py','paper_alpha/server/daily_snapshot_guard.py','paper_alpha/server/daily_revision_bindings.py','scripts/run_daily_research.py','paper_alpha/evidence.py','paper_alpha/market_diagnostics.py','paper_alpha/server/db.py','paper_alpha/server/service.py','paper_alpha/server/runner.py','paper_alpha/server/execution_lifecycle.py','paper_alpha/server/research_guard.py','paper_alpha/server/observation_identity.py','paper_alpha/server/maintenance.py','paper_alpha/server/research_session_experiments.py','paper_alpha/server/research_sessions.py','paper_alpha/server/research_session_execution.py','paper_alpha/server/research_jobs.py','paper_alpha/server/paper_segments.py','paper_alpha/server/paper_materials.py','paper_alpha/server/material_context.py','paper_alpha/server/provider_context.py','paper_alpha/server/provider_research_context.py','paper_alpha/server/research_draft_validation.py','paper_alpha/server/provider_field_semantics.py','paper_alpha/server/response_schemas.py','paper_alpha/server/api.py','paper_alpha/server/mutations.py')

def _safe(path):
    value=Path(path)
    require('..' not in value.parts and not any(p.is_symlink() for p in (value,*value.parents)),'Unsafe daily path')
    return value.absolute()

def _read(path,limit=MAX_RAW_BYTES):
    path=_safe(path);_,stream,before=_open_regular(path,limit)
    with stream:
        data=stream.read(limit+1);require(len(data)<=limit,'Bounded input exceeded');_stable(path,stream,before)
    return data

def _json(path):
    def pairs(items):
        out={}
        for k,v in items:
            require(k not in out,'Duplicate JSON key');out[k]=v
        return out
    def bad(value): raise ValueError('Nonfinite JSON')
    return json.loads(_read(path),object_pairs_hook=pairs,parse_constant=bad)

def _sha(path):return hashlib.sha256(_read(path)).hexdigest()

def code_identity():
    root=Path(__file__).resolve().parents[1]
    files={p:_sha(root/p) for p in CODE_FILES};return {'files':files,'digest':digest(files)}

class _ReadOnlyHost:
    def __init__(self,root,db_path):
        self.root=_safe(root);self.db_path=_safe(db_path)
        require(self.db_path.is_file() and self.db_path.is_relative_to(self.root),'Existing managed database required')
    def _read(self,sql,args=()):
        with closing(sqlite3.connect(self.db_path.as_uri()+'?mode=ro',uri=True)) as c:
            c.row_factory=sqlite3.Row;return [dict(r) for r in c.execute(sql,args)]
    def _fetch(self,table,identity):
        require(table in {'datasets','papers','researches','revisions','runs','attempts'},'Closed read table required')
        rows=self._read('SELECT * FROM '+table+' WHERE id=?',(identity,));require(bool(rows),'Business resource missing');return rows[0]

def _assert_descriptor(descriptor):
    keys={'schema_version','task','inputs','paper_id','paper_sha256','paper_document_digest','workspace','db_path','run_id','attempt_id','worker_id'}
    require(type(descriptor) is dict and set(descriptor)==keys and type(descriptor['schema_version']) is int and descriptor['schema_version']==2,'Closed daily descriptor required')
    require(descriptor['inputs']=={'contract':'contract.json','csv':'market.csv','source_receipt':'source_receipt.json'},'Closed materialized input names required')
    store=_ReadOnlyHost(descriptor['workspace'],descriptor['db_path'])
    run=store._fetch('runs',descriptor['run_id']);attempt=store._fetch('attempts',descriptor['attempt_id'])
    require(run['attempt_id']==descriptor['attempt_id'] and run['worker_id']==descriptor['worker_id'] and run['status']=='running' and attempt['run_id']==run['id'] and attempt['worker_id']==descriptor['worker_id'],'Active original worker ownership required')
    revision=store._fetch('revisions',run['revision_id']);require(digest(json.loads(revision['task']))==digest(descriptor['task']),'Queued revision differs')
    from .server.daily_revision_bindings import assert_revision
    with closing(sqlite3.connect(store.db_path.as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory=sqlite3.Row;assert_revision(c,revision)
    source=assert_daily_task(store,descriptor['task'])
    research=store._fetch('researches',revision['research_id']);paper=store._fetch('papers',research['paper_id'])
    require(paper['id']==descriptor['paper_id'] and paper['sha256']==descriptor['paper_sha256'] and digest(json.loads(paper['document']))==descriptor['paper_document_digest'],'Queued paper differs')
    return store,source

def execute_daily_tool(job):
    require(type(job) is dict and set(job)=={'job_version','descriptor_path','action','expression','candidate_id','output_dir'},'Closed daily tool job required')
    require(job['job_version']==RUN_VERSION and job['action']=='compute_evaluate','Unsupported daily tool operation')
    descriptor_path=_safe(job['descriptor_path']);descriptor=_json(descriptor_path)
    store,source=_assert_descriptor(descriptor) # BEFORE first numeric conversion.
    task=descriptor['task'];base=descriptor_path.parent
    candidate=next((c for c in task['candidates'] if c['id']==job['candidate_id']),None)
    require(candidate is not None and candidate['expression']==job['expression'],'Tool candidate is not frozen')
    for key,path in descriptor['inputs'].items():
        require(_sha(base/path)==_sha(source['paths'][key]),'Materialized input differs from registration')
    paper=_json(base/'paper.json');verify_paper(paper,base/'paper.pdf')
    require(_sha(base/'paper.pdf')==descriptor['paper_sha256'] and digest(paper)==descriptor['paper_document_digest'],'Materialized paper changed')
    for e in task['evidence']:match_evidence(Evidence(**e),paper)
    source_before=digest(source)
    preflight=inspect_daily_task(task,json.loads(source['dataset']['metadata']))
    require(not any(c['status']=='blocked' and c['candidate_id']==candidate['id'] for c in preflight['candidates']),'Candidate has no legal development rows')
    view=precheck_development(base/'contract.json',base/'market.csv',base/'source_receipt.json',allow_whole_byte_hash=True,required_fields=next(h['required_fields'] for h in task['hypotheses'] if h['id']==candidate['hypothesis_id']))
    require(view.contract.to_dict()['origin']=='engineering_fixture','Real execution qualification pending')
    panels=view.panels
    prefix_metadata=json.loads(source['dataset']['metadata'])
    for panel in panels.values():panel.attrs['market_metadata']=deepcopy(prefix_metadata)
    factor=compute_expression(candidate['expression'],panels)
    result=evaluate_development(factor,panels,task['evaluation'],view.contract.calendar)
    result.update(expression=candidate['expression'],expression_diagnostics=deepcopy(factor.attrs.get('expression_diagnostics',{})))
    require(digest(_assert_descriptor(descriptor)[1])==source_before,'Authority changed during tool execution')
    output=_safe(job['output_dir']);require(not output.exists(),'Candidate output already exists');output.mkdir(parents=True,exist_ok=False)
    factor.to_csv(output/'factor.csv',index_label='date',float_format='%.17g')
    atomic_json(output/'result.json',result)
    return result

def _snapshot_public_inputs(out,descriptor,source,descriptor_path,code):
    # No descriptor, full/raw CSV or host paths enter public output snapshots.
    folder=out/'inputs';folder.mkdir(exist_ok=False)
    atomic_json(folder/'task.json',descriptor['task'])
    metadata=json.loads(source['dataset']['metadata']);atomic_json(folder/'metadata.json',metadata)
    paper=_json(descriptor_path.parent/'paper.json')
    require(digest(paper)==descriptor['paper_document_digest'],'Public paper document changed')
    atomic_json(folder/'paper.json',paper)
    pdf=_read(descriptor_path.parent/'paper.pdf')
    require(hashlib.sha256(pdf).hexdigest()==descriptor['paper_sha256'],'Public paper PDF changed')
    (folder/'paper.pdf').write_bytes(pdf)
    root=Path(__file__).resolve().parents[1]
    for relative,sha in code['files'].items():
        raw=_read(root/relative)
        require(hashlib.sha256(raw).hexdigest()==sha,'Runtime source changed during snapshot')
        target=out/'source'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    return digest(metadata)

def _manifest(out,state,descriptor,code):
    files={p.relative_to(out).as_posix():_sha(p) for p in sorted(out.rglob('*')) if p.is_file() and p.name!='manifest.json'}
    value={'schema_version':2,'run_version':RUN_VERSION,'kind':descriptor['task']['execution']['kind'],
           'task_digest':digest(descriptor['task']),'execution':descriptor['task']['execution'],'state_digest':digest(state),
           'descriptor_digest':digest(descriptor),'run_id':descriptor['run_id'],'attempt_id':descriptor['attempt_id'],'worker_id':descriptor['worker_id'],'mode':state['mode'],
           'paper_sha256':descriptor['paper_sha256'],'paper_document_digest':descriptor['paper_document_digest'],
           'prefix_metadata_digest':digest(_json(out/'inputs/metadata.json')),'code':code,'signature':{'code':deepcopy(code['files']),'inputs':{'paper_pdf':descriptor['paper_sha256']},'mode':state['mode']},
           'snapshot_files':{name:sha for name,sha in files.items() if name.startswith(('inputs/','source/'))},
           'files':files,'final_numeric_rows_parsed':0,
           'evaluation_scope':'development_prefix_validation_only'}
    atomic_json(out/'manifest.json',value);return value

def verify_daily_run(out_dir,*,expected_task=None,expected_manifest_sha256=None,require_current_code=False):
    out=_safe(out_dir);manifest=_json(out/'manifest.json');state=_json(out/'state.json')
    require(manifest['schema_version']==2 and manifest['run_version']==RUN_VERSION,'Unsupported daily manifest')
    if expected_manifest_sha256 is not None:require(_sha(out/'manifest.json')==expected_manifest_sha256,'Pinned daily manifest changed')
    require(digest(state)==manifest['state_digest'] and digest(state['task'])==manifest['task_digest'],'Daily state/task digest changed')
    if expected_task is not None:require(digest(expected_task)==manifest['task_digest'],'Daily output belongs to another task')
    current_files={p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file() and p.name!='manifest.json'}
    require(current_files==set(manifest['files']),'Daily output inventory changed')
    for name,sha in manifest['files'].items():require(_sha(out/name)==sha,'Daily output bytes changed')
    require(manifest['final_numeric_rows_parsed']==0 and manifest['evaluation_scope']=='development_prefix_validation_only','Final scope invalid')
    require(state['task']['execution']==manifest['execution'],'Execution binding changed')
    require(type(state['mode']) is str and state['mode'] in {'fixed','normalized_fixed','agent'} and manifest['mode']==state['mode'],'Executed mode differs from state')
    results={}
    for c in state['candidates']:
        if c['status'] in {'evaluated','not_evaluable'}:
            result=_json(out/'candidates'/c['id']/'attempt-1'/'result.json')
            require(digest(result)==c['result_digest'] and digest(result)==digest(c['result']),'Candidate result differs from actual tool artifact')
            require(result['evaluation_scope']=='development_prefix_validation_only' and c['status']==result['status'],'Invalid evaluation scope/status')
            expected_artifacts={p.relative_to(out).as_posix():_sha(p) for p in (out/'candidates'/c['id']/'attempt-1').iterdir() if p.is_file()}
            require(c['artifacts']==expected_artifacts,'Candidate artifact bindings changed')
            results[c['id']]=digest(result)
    report=_json(out/'report.json');require(report['state_digest']==digest(state) and report['candidate_results']==results,'Report differs from actual state/results')
    metrics={c['id']:c['result']['metrics'] for c in state['candidates'] if c['status'] in {'evaluated','not_evaluable'}}
    require(digest(report['metrics'])==digest(metrics),'Reported metrics differ from actual tool metrics')
    require(set(manifest['code'])=={'files','digest'} and set(manifest['code']['files'])==set(CODE_FILES) and digest(manifest['code']['files'])==manifest['code']['digest'],'Code inventory invalid')
    snapshots={name:sha for name,sha in manifest['files'].items() if name.startswith(('inputs/','source/'))}
    expected_snapshots={'inputs/task.json','inputs/paper.json','inputs/paper.pdf','inputs/metadata.json'}|{'source/'+name for name in CODE_FILES}
    require(set(snapshots)==expected_snapshots and manifest['snapshot_files']==snapshots,'Closed public snapshot inventory changed')
    require(digest(manifest['signature'])==digest({'code':manifest['code']['files'],'inputs':{'paper_pdf':manifest['paper_sha256']},'mode':state['mode']}),'Recorded source/paper/mode signature differs')
    for name,sha in manifest['code']['files'].items():require(snapshots['source/'+name]==sha,'Recorded source differs from execution source')
    require(digest(_json(out/'inputs/task.json'))==manifest['task_digest'],'Public task snapshot differs')
    require(_sha(out/'inputs/paper.pdf')==manifest['paper_sha256'] and digest(_json(out/'inputs/paper.json'))==manifest['paper_document_digest'],'Public paper snapshot differs')
    metadata=_json(out/'inputs/metadata.json')
    require(digest(metadata)==manifest['prefix_metadata_digest'] and metadata['data_kind']=='daily_development_snapshot','Registered prefix metadata differs')
    for c in state['candidates']:
        if c['status'] in {'evaluated','not_evaluable'}:
            require(digest(c['result']['market_metadata'])==digest(metadata) and digest(c['result']['config'])==digest(state['task']['evaluation']),'Actual result differs from its frozen metadata/config')
    current=code_identity()==manifest['code']
    if require_current_code:require(current,'Daily code changed since execution')
    return {'valid':True,'verified':True,'status':state['status'],'state_digest':digest(state),'task_digest':manifest['task_digest'],'manifest_sha256':_sha(out/'manifest.json'),
            'final_numeric_rows_parsed':0,'candidate_results':results,'evaluation_scope':'development_prefix_validation_only','current_source_matches':current}

def run_daily_task(task_path,out_dir,mode='fixed',resume=False):
    require(mode in {'fixed','normalized_fixed','agent'} and type(resume) is bool,'Invalid daily mode')
    task_path=_safe(task_path);descriptor=_json(task_path);out=_safe(out_dir)
    if out.exists():
        require(resume,'Output already exists');verified=verify_daily_run(out,expected_task=descriptor['task'],require_current_code=True)
        state=_json(out/'state.json');require(state['status'] in {'completed','failed','budget_exhausted'},'Partial attempt cannot automatically recompute');return state
    require(not resume,'No original daily output to resume')
    store,source=_assert_descriptor(descriptor)
    require(mode==store._fetch('runs',descriptor['run_id'])['mode'],'Daily mode differs from queued run')
    task=descriptor['task'];code=code_identity()
    out.mkdir(parents=True,exist_ok=False);started=time.monotonic()
    state={'schema_version':1,'run_version':RUN_VERSION,'status':'running','task':task,'mode':mode,'candidates':[],
           'tool_calls':0,'elapsed_seconds':0.0,'events':[],'error':None,'evaluation_scope':'development_prefix_validation_only','final_numeric_rows_parsed':0,
           'created_at':datetime.now(timezone.utc).isoformat()}
    state['events'].append({'sequence':1,'kind':'daily_workflow_started','run_id':descriptor['run_id'],'attempt_id':descriptor['attempt_id']})
    atomic_json(out/'state.json',state);(out/'events.jsonl').write_text(json.dumps(state['events'][0])+'\n',encoding='utf-8')
    try:
        _snapshot_public_inputs(out,descriptor,source,task_path,code)
        for candidate in task['candidates']:
            if state['tool_calls']>=task['budget']['max_tool_calls'] or time.monotonic()-started>task['budget']['max_seconds']:raise TimeoutError('Daily cumulative tool/wall budget exhausted')
            state['tool_calls']+=1;record={**deepcopy(candidate),'status':'running','attempts':[],'artifacts':{}};state['candidates'].append(record);atomic_json(out/'state.json',state)
            try:
                from .worker import execute
                result=execute({'job_version':RUN_VERSION,'descriptor_path':str(task_path),'action':'compute_evaluate','expression':candidate['expression'],'candidate_id':candidate['id'],'output_dir':str(out/'candidates'/candidate['id']/'attempt-1')})
                record.update(status=result['status'],result=result,result_digest=digest(result),attempts=[{'number':1,'expression':candidate['expression'],'status':result['status'],'tool_call':state['tool_calls']}],artifacts={p.relative_to(out).as_posix():_sha(p) for p in (out/'candidates'/candidate['id']/'attempt-1').iterdir() if p.is_file()})
            except (ValueError,OSError,KeyError,TypeError) as exc:
                record.update(status='failed',error={'code':getattr(exc,'code','daily_tool_error'),'message':str(exc)},attempts=[{'number':1,'expression':candidate['expression'],'status':'failed','tool_call':state['tool_calls']}])
            atomic_json(out/'state.json',state)
        _assert_descriptor(descriptor);require(code_identity()==code,'Code changed during daily attempt')
        state['status']='completed'
    except (ValueError,OSError,KeyError,TypeError,TimeoutError) as exc:
        state.update(status='budget_exhausted' if isinstance(exc,TimeoutError) else 'failed',error={'code':getattr(exc,'code','daily_run_error'),'message':str(exc)})
    state['elapsed_seconds']=float(time.monotonic()-started)
    state['finished_at']=datetime.now(timezone.utc).isoformat();atomic_json(out/'state.json',state)
    results={c['id']:c['result_digest'] for c in state['candidates'] if c['status'] in {'evaluated','not_evaluable'}}
    atomic_json(out/'report.json',{'schema_version':2,'state_digest':digest(state),'candidate_results':results,'metrics':{c['id']:c['result']['metrics'] for c in state['candidates'] if c['status'] in {'evaluated','not_evaluable'}},'test_freshness':'unknown','real_source_ready':False,'scientific_score':None})
    (out/'report.md').write_text('# 注册日频开发评估\n\n工程夹具，validation-only；最终数字读取 0，科学质量未验证。\n\n```json\n'+json_text({c['id']:c.get('result',{}).get('metrics') for c in state['candidates']})+'```\n',encoding='utf-8')
    _manifest(out,state,descriptor,code);return state

def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('task_path');p.add_argument('--out',required=True);p.add_argument('--mode',default='fixed');p.add_argument('--resume',action='store_true');a=p.parse_args()
    state=run_daily_task(a.task_path,a.out,a.mode,a.resume);return 0 if state['status']=='completed' else 2

if __name__=='__main__':sys.exit(main())
