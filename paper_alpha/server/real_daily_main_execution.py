"""Main queue hooks. Host retains authority; child gets only local snapshots."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from ..storage import atomic_json, digest, json_text
from ..real_daily_main_workflow import (RUN_VERSION, PRODUCER_KIND, INVENTORY_VERSION, INPUTS,
    FILES, assessment_snapshot, verify_main_run, read, value, decode_output_json)
from .real_daily_source_handles import parse_json, read_managed
from .real_daily_workspace_authority import _rows

PIN_KEYS=frozenset({'schema_version','producer_kind','inventory_version','run_id','attempt_id','worker_id',
    'revision_id','registration_id','registration_digest','task_digest','execution_digest','paper_sha256',
    'paper_document_digest','output_relative','manifest_sha256','files','created_at'})


def need(condition, message):
    if not condition:raise ValueError(message)


def assert_run_permission(store,run,connection=None):
    from .real_daily_main_research import assert_main_task
    task=json.loads(store._fetch('revisions',run['revision_id'])['task'])
    return assert_main_task(store,task,connection=connection,require_current_permission=True)


def materialize_main_job(store,job,attempt_dir):
    from .real_daily_main_research import assert_main_task
    task=json.loads(job['revision']['task']) if isinstance(job['revision']['task'],str) else job['revision']['task']
    source=assert_main_task(store,task,require_current_permission=True)
    folder=Path(attempt_dir).absolute();folder.mkdir(parents=True,exist_ok=True)
    reg=source['registration'];paper=store._fetch('papers',job['research']['paper_id'])
    pdf,info=read_managed(store.root,Path(paper['pdf_path']).relative_to(store.root).as_posix(),maximum=32*1024*1024,expected_sha256=paper['sha256'])
    (folder/INPUTS['paper_pdf']).write_bytes(pdf)
    atomic_json(folder/INPUTS['paper'],json.loads(paper['document']))
    atomic_json(folder/INPUTS['task'],task)
    for key,field in (('prefix','prefix_sha256'),('receipt','receipt_sha256')):
        raw,_=read_managed(store.root,Path(source['paths'][key]).relative_to(store.root).as_posix(),maximum=32*1024*1024,expected_sha256=reg[field])
        (folder/INPUTS[key]).write_bytes(raw)
    atomic_json(folder/INPUTS['registration'],reg)
    atomic_json(folder/INPUTS['assessment'],assessment_snapshot(source))
    atomic_json(folder/INPUTS['catalog'],source['catalog'])
    descriptor={'schema_version':3,'job_version':RUN_VERSION,'task':task,'inputs':INPUTS,
        'paper_id':paper['id'],'paper_sha256':paper['sha256'],'paper_document_digest':digest(json.loads(paper['document'])),
        'run_id':job['id'],'attempt_id':job['attempt_id'],'worker_id':job['worker_id'],
        'revision_id':job['revision']['id'],'registration_digest':reg['registration_digest']}
    atomic_json(folder/'worker-job.json',descriptor)
    assert_main_task(store,task,require_current_permission=True)
    return {'task_path':str(folder/'worker-job.json'),'execution_kind':task['execution']['kind'],'binding_digest':digest(task['execution'])}


def bind_main_output(store,run,state,manifest,output,*,require_current_code=True):
    from .real_daily_main_research import assert_main_task
    revision=store._fetch('revisions',run['revision_id']);task=json.loads(revision['task'])
    source=assert_main_task(store,task,require_current_permission=require_current_code)
    verify_main_run(output,expected_task=task,require_current_code=require_current_code)
    need(manifest['run_id']==run['id'] and manifest['attempt_id']==run['attempt_id'] and manifest['revision_id']==revision['id'] and
         manifest['worker_id']==run['worker_id'] and manifest['mode']==run['mode'],'Main output belongs to another run/attempt/worker/mode')
    need(manifest['execution']==source['execution'] and manifest['registration_digest']==source['registration']['registration_digest'],'Main output registration changed')
    research=store._fetch('researches',revision['research_id']);paper=store._fetch('papers',research['paper_id'])
    need(manifest['paper_sha256']==paper['sha256'] and manifest['paper_document_digest']==digest(json.loads(paper['document'])),'Main output paper changed')


def assert_result_binding_history(connection, rows, registrations):
    from ..real_daily_main_task import is_main_task
    seen=set()
    for row in rows:
        payload=parse_json(row['payload_json'])
        need(set(payload)==PIN_KEYS and type(payload['schema_version']) is int and payload['schema_version']==1 and
             payload['producer_kind']==PRODUCER_KIND and payload['inventory_version']==INVENTORY_VERSION and
             row['producer_digest']==digest(payload),'Main producer binding changed')
        for key in ('attempt_id','run_id','revision_id','registration_id','created_at'):need(payload[key]==row[key],'Main producer column changed')
        run=next(iter(_rows(connection, 'SELECT * FROM runs WHERE id=?',(row['run_id'],))), None)
        attempt=next(iter(_rows(connection, 'SELECT * FROM attempts WHERE id=?',(row['attempt_id'],))), None)
        revision=next(iter(_rows(connection, 'SELECT * FROM revisions WHERE id=?',(row['revision_id'],))), None)
        reg=registrations.get(row['registration_id'])
        need(run is not None and attempt is not None and revision is not None and reg is not None,'Main producer resource missing')
        task=parse_json(revision['task'])
        need(is_main_task(task) and digest(task)==revision['digest']==payload['task_digest'] and
             task['execution']['registration_id']==reg['id'] and task['execution']['registration_digest']==payload['registration_digest']==reg['registration_digest'] and
             digest(task['execution'])==payload['execution_digest'],'Main producer task/registration changed')
        need(attempt['run_id']==run['id'] and run['revision_id']==revision['id'] and attempt['worker_id']==payload['worker_id'] and
             attempt['status']=='completed' and (parse_json(attempt['verification']) or {}).get('verified') is True,'Main producer attempt changed')
        need(payload['output_relative']=='runs/'+run['id']+'/attempts/'+attempt['id']+'/output' and set(payload['files'])==FILES,'Main producer locator/inventory changed')
        from ..real_daily_main_metadata import pin
        for key in ('manifest_sha256','paper_sha256','paper_document_digest','task_digest','execution_digest','registration_digest'):pin(payload[key])
        for sha in payload['files'].values():pin(sha)
        seen.add(attempt['id'])
    for row in _rows(connection, "SELECT a.id,v.task FROM attempts a JOIN runs r ON r.id=a.run_id JOIN revisions v ON v.id=r.revision_id WHERE a.status='completed'"):
        if is_main_task(parse_json(row['task'])):need(row['id'] in seen,'Completed main attempt has no immutable producer binding')
    return True


def record_main_result(store,connection,run,state,manifest,output):
    from .real_daily_prefix_registration import assert_prefix_history
    # Called after permission check and terminal row updates, in the same finish transaction.
    payload={'schema_version':1,'producer_kind':PRODUCER_KIND,'inventory_version':INVENTORY_VERSION,
        'run_id':run['id'],'attempt_id':run['attempt_id'],'worker_id':run['worker_id'],'revision_id':run['revision_id'],
        'registration_id':state['task']['execution']['registration_id'],'registration_digest':manifest['registration_digest'],
        'task_digest':digest(state['task']),'execution_digest':digest(state['task']['execution']),
        'paper_sha256':manifest['paper_sha256'],'paper_document_digest':manifest['paper_document_digest'],
        'output_relative':Path(output).relative_to(store.root).as_posix(),
        'manifest_sha256':hashlib.sha256(read(output,'manifest.json')).hexdigest(),'files':deepcopy(manifest['files']),
        'created_at':state['finished_at']}
    connection.execute('INSERT INTO rdw_result_bindings VALUES (?,?,?,?,?,?,?)',
        (payload['attempt_id'],payload['run_id'],payload['revision_id'],payload['registration_id'],digest(payload),json_text(payload),payload['created_at']))
    assert_prefix_history(connection)
    return payload


def verify_saved_main_result(store,connection,run,*,recorded_root=None):
    from .real_daily_prefix_registration import assert_prefix_history
    state=assert_prefix_history(connection)
    row=connection.execute('SELECT * FROM rdw_result_bindings WHERE attempt_id=?',(run['attempt_id'],)).fetchone()
    need(row is not None,'Immutable main producer binding missing')
    expected=parse_json(row['payload_json']);output=store.root/expected['output_relative']
    revision=connection.execute('SELECT * FROM revisions WHERE id=?',(run['revision_id'],)).fetchone()
    checked=verify_main_run(output,expected_task=parse_json(revision['task']),expected_manifest_sha256=expected['manifest_sha256'],expected_files=expected['files'],require_current_code=False)
    attempt=connection.execute('SELECT * FROM attempts WHERE id=?',(run['attempt_id'],)).fetchone()
    saved_state=value(output,'state.json')
    need(attempt is not None and attempt['run_id']==run['id'] and attempt['worker_id']==expected['worker_id'] and
         run['worker_id']==expected['worker_id'] and attempt['status']==run['status']=='completed' and
         digest(decode_output_json(run['state']))==attempt['state_digest']==checked['state_digest']==digest(saved_state),
         'Displayed main state differs from immutable producer output')
    for stored in (run['verification'],attempt['verification']):
        verification=parse_json(stored)
        need(verification==checked,'Saved main verification differs from immutable producer')
    artifacts=list(connection.execute('SELECT * FROM artifacts WHERE attempt_id=?',(run['attempt_id'],)))
    need(len(artifacts)==14 and {a['name'] for a in artifacts}==FILES|{'manifest.json'},'Main public export inventory changed')
    for artifact in artifacts:
        name=artifact['name'];relative='runs/'+run['id']+'/attempts/'+run['attempt_id']+'/exports/'+name
        need(artifact['run_id']==run['id'] and artifact['path']==str(Path(recorded_root or store.root)/relative),'Main public export locator changed')
        exported,info=read_managed(store.root,relative,maximum=32*1024*1024)
        need(info['sha256']==artifact['sha256'] and info['size_bytes']==artifact['size'],'Main public export bytes changed')
        original=read(output,name)
        if name.endswith('.json'):
            need(decode_output_json(exported)==store._public(decode_output_json(original)),'Main public JSON differs from actual output')
        elif name.endswith(('.md','.txt')):
            need(exported.decode('utf-8')==store._public(original.decode('utf-8')),'Main public report differs from actual output')
        else:
            need(exported==original,'Main public binary differs from actual output')
    return checked
