"""Thin host hooks for registered daily research; no Store initialization."""
from copy import deepcopy
import json
from pathlib import Path
from ..daily_contract import parse_contract
from ..daily_task_v2 import CORE_KEYS, execution_binding, is_daily_task, parse_daily_task
from ..storage import digest, atomic_json
from .daily_snapshots import DailySnapshots
from .provider_catalog import catalog_from_daily_contract


def resolve_registered_daily_source(store, registration_id, connection=None):
    current = DailySnapshots(store).compute_inputs(registration_id)
    row = current['registration']
    if connection is not None:
        saved=connection.execute('SELECT * FROM daily_snapshot_registrations WHERE id=?',(registration_id,)).fetchone()
        if saved is None or digest(dict(saved)) != digest(row):
            raise ValueError('Registration changed in transaction snapshot')
    dataset=store._fetch('datasets',row['registered_dataset_id'])
    contract=parse_contract(json.loads(row['contract_json']))
    return {'registration':row,'dataset':dataset,'contract':contract.to_dict(),
            'catalog':catalog_from_daily_contract(contract),'execution':execution_binding(row,dataset['id']),
            'paths':{k:str(v) for k,v in current['paths'].items()},'guard':current['guard']}


def source_for_dataset(store, dataset_id, registration_id=None, connection=None):
    if registration_id is None:
        rows=store._read("SELECT id FROM daily_snapshot_registrations WHERE registered_dataset_id=? AND status='registered' ORDER BY created_at,id LIMIT 1",(dataset_id,))
        if not rows: raise ValueError('Dataset has no current daily registration')
        registration_id=rows[0]['id']
    source=resolve_registered_daily_source(store,registration_id,connection)
    if source['dataset']['id'] != dataset_id: raise ValueError('Registration belongs to another dataset')
    return source


def freeze_task(store, title, dataset_id, task, registration_id=None, connection=None):
    source=source_for_dataset(store,dataset_id,registration_id,connection)
    if is_daily_task(task):
        value=deepcopy(task)
        if value['title'] != title: raise ValueError('Daily title differs from host research')
    else:
        if type(task) is not dict or set(task)-CORE_KEYS-{'schema_version','title'}: raise ValueError('Unexpected daily core keys')
        if task.get('schema_version',1) != 1: raise ValueError('Unsupported input task schema')
        value={'schema_version':2,'title':title,**{k:deepcopy(task[k]) for k in CORE_KEYS},'execution':source['execution']}
    return parse_daily_task(value,source['execution'],source['contract']['calendar']['sessions'],source['catalog'])


def assert_daily_task(store,task,dataset_id=None,connection=None):
    if not is_daily_task(task): raise ValueError('Versioned daily task required')
    source=source_for_dataset(store,dataset_id or task['execution']['dataset_id'],task['execution']['registration_id'],connection)
    checked=parse_daily_task(task,source['execution'],source['contract']['calendar']['sessions'],source['catalog'])
    if digest(checked) != digest(task): raise ValueError('Stored daily task is not canonical')
    return source


def materialize_daily_job(store, job, attempt_dir):
    import shutil
    from ..evidence import sha256
    task=json.loads(job['revision']['task']) if isinstance(job['revision']['task'],str) else job['revision']['task']
    source=assert_daily_task(store,task)
    folder=Path(attempt_dir)
    if any(p.is_symlink() for p in (folder,*folder.parents)) or '..' in folder.parts: raise ValueError('Unsafe attempt path')
    folder.mkdir(parents=True,exist_ok=True)
    paths={}
    for key,name in (('contract','contract.json'),('csv','market.csv'),('source_receipt','source_receipt.json')):
        target=folder/name
        if target.exists(): raise ValueError('Attempt input already exists')
        from ..evidence import sha256
        before=sha256(source['paths'][key]); shutil.copyfile(source['paths'][key],target)
        if sha256(target)!=before or sha256(source['paths'][key])!=before: raise ValueError('Input changed during materialization')
        paths[key]=name
    paper=store._fetch('papers',job['research']['paper_id'])
    if sha256(paper['pdf_path'])!=paper['sha256']: raise ValueError('Registered paper changed')
    shutil.copyfile(paper['pdf_path'],folder/'paper.pdf')
    if sha256(folder/'paper.pdf')!=paper['sha256'] or sha256(paper['pdf_path'])!=paper['sha256']: raise ValueError('Paper changed during materialization')
    atomic_json(folder/'paper.json',json.loads(paper['document']))
    descriptor={'schema_version':2,'task':task,'inputs':paths,'paper_id':paper['id'],
                'paper_sha256':paper['sha256'],'paper_document_digest':digest(json.loads(paper['document'])),'workspace':str(store.root),'db_path':str(store.db_path),
                'run_id':job['id'],'attempt_id':job['attempt_id'],'worker_id':job['worker_id']}
    atomic_json(folder/'task.json',descriptor)
    assert_daily_task(store,task)
    return {'task_path':str(folder/'task.json'),'execution_kind':task['execution']['kind'],'binding_digest':digest(task['execution'])}


def inspect_daily_task(task,metadata):
    import ast
    from ..expressions import validate_expression,repair_expression,ExpressionError
    if not is_daily_task(task) or metadata.get('data_kind')!='daily_development_snapshot': raise ValueError('Registered daily metadata required')
    fields=set(metadata['columns'])-{'date','asset'}
    if fields != {'open','high','low','close','volume'}: raise ValueError('Actual registered daily fields differ')
    fields.add('returns'); dates=metadata['calendar_dates']; bounds=task['evaluation']['splits']['validation']
    start=dates.index(bounds['start']);last=dates.index(bounds['end'])-2
    def lookback(node):
        if isinstance(node,ast.Name):return 1 if node.id=='returns' else 0
        if isinstance(node,ast.Call) and node.func.id in {'delay','ts_delta','ts_corr','ts_mean','ts_std','ts_min','ts_max','ts_sum'}:
            return max((lookback(a) for a in node.args[:-1]),default=0)+(node.args[-1].value if node.func.id in {'delay','ts_delta'} else node.args[-1].value-1)
        return max((lookback(c) for c in ast.iter_child_nodes(node)),default=0)
    hypotheses={h['id']:h for h in task['hypotheses']};rows=[]
    for c in task['candidates']:
        row={'candidate_id':c['id'],'status':'ready','code':'static_preflight_passed','expression_changed':False,'warmup_rows':None,'missing_fields':[]}
        missing=sorted(set(hypotheses[c['hypothesis_id']]['required_fields'])-fields)
        if missing:row.update(status='blocked',code='required_field_missing',missing_fields=missing)
        else:
            try:
                v=validate_expression(c['expression'],fields)
                if set(v['used_fields'])-set(hypotheses[c['hypothesis_id']]['required_fields']):raise ExpressionError('Undeclared field','undeclared_field')
                warmup=lookback(ast.parse(v['canonical_expression'],mode='eval').body);row['warmup_rows']=warmup
                if warmup>last:row.update(status='blocked',code='no_validation_rows_after_warmup')
                elif warmup>start:row.update(status='warning',code='partial_validation_warmup')
            except (ExpressionError,SyntaxError) as exc:row.update(status='blocked',code=getattr(exc,'code','syntax_error'),message=str(exc))
        rows.append(row)
    return {'dataset_version':metadata['version'],'fields':sorted(fields),'candidates':rows,'scope':'Static prefix-calendar suitability; no substitutions, final numeric data or economic validation.'}


def bind_daily_output(store,run,state,manifest,output,*,require_current_code=True):
    from ..daily_workflow_v2 import verify_daily_run
    revision=store._revision(store._fetch('revisions',run['revision_id']))
    verified=verify_daily_run(output,expected_task=revision['task'],require_current_code=require_current_code)
    if digest(state)!=manifest['state_digest'] or manifest['run_id']!=run['id'] or manifest['attempt_id']!=run['attempt_id']: raise ValueError('Published output belongs to another run or attempt')
    if state['mode']!=run['mode'] or manifest['mode']!=run['mode'] or manifest['signature']['mode']!=run['mode']: raise ValueError('Executed mode differs from queued run')
    attempt=store._fetch('attempts',run['attempt_id'])
    if manifest['worker_id']!=run['worker_id'] or manifest['worker_id']!=attempt['worker_id'] or attempt['run_id']!=run['id']: raise ValueError('Executed worker differs from queued attempt')
    source=assert_daily_task(store,revision['task'])
    if manifest['execution'] != source['execution']: raise ValueError('Published registration binding changed')
    if manifest['prefix_metadata_digest']!=digest(json.loads(source['dataset']['metadata'])): raise ValueError('Published prefix metadata differs from registered snapshot')
    research=store._fetch('researches',revision['research_id']);paper=store._fetch('papers',research['paper_id'])
    if manifest['paper_sha256']!=paper['sha256'] or manifest['paper_document_digest']!=digest(json.loads(paper['document'])): raise ValueError('Published paper identity changed')
    return verified
