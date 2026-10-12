"""Attempt-local OHLC execution. No database, provider or original source IO."""
from copy import deepcopy
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
import os
import stat
from pathlib import Path
import sys
import time

from .storage import atomic_json, digest, json_text
from .evidence import verify_paper, match_evidence
from .contracts import Evidence
from .server.real_daily_source_handles import read_managed, parse_json
from .server.real_daily_prefix_registration import main_execution_binding

RUN_VERSION = 'real-daily-main-worker-v1'
PRODUCER_KIND = 'main_real_daily_ohlc_development_v1'
INVENTORY_VERSION = 'real-daily-main-inventory-v1'
INPUTS = {'paper_pdf':'paper.pdf','paper':'paper.json','task':'task.json','prefix':'prefix.csv',
          'receipt':'prefix_receipt.json','registration':'registration.json','assessment':'assessment.json','catalog':'catalog.json'}
FILES = frozenset({'inputs/'+name for name in INPUTS.values()} | {'candidate.json','factor.csv','result.json','report.md','state.json'})
CODE_FILES = ('paper_alpha/real_daily_main_workflow.py','paper_alpha/real_daily_main_task.py',
    'paper_alpha/real_daily_method.py','paper_alpha/expressions.py','paper_alpha/evaluation.py',
    'paper_alpha/contracts.py','paper_alpha/evidence.py','paper_alpha/storage.py','paper_alpha/vendor/operators.py',
    'paper_alpha/real_daily_main_metadata.py','paper_alpha/real_daily_web_contract.py',
    'paper_alpha/server/real_daily_source_handles.py','paper_alpha/server/real_daily_prefix_registration.py')
DESCRIPTOR_KEYS = frozenset({'schema_version','job_version','task','inputs','paper_id','paper_sha256',
    'paper_document_digest','run_id','attempt_id','worker_id','revision_id','registration_digest'})


def need(condition, message):
    if not condition:
        raise ValueError(message)


def read(root, name, limit=32*1024*1024):
    return read_managed(Path(root).absolute(),name,maximum=limit)[0]


MAX_OUTPUT_JSON = 16 * 1024 * 1024


def decode_output_json(raw):
    """Bounded saved computation JSON; metadata keeps its separate 2 MiB limit."""
    need(type(raw) in (bytes,str), 'Saved output JSON required')
    need(len(raw if type(raw) is bytes else raw.encode('utf-8')) <= MAX_OUTPUT_JSON, 'Saved output JSON exceeds 16 MiB')
    def pairs(items):
        out={}
        for key,item in items:
            need(key not in out,'Duplicate saved output key')
            out[key]=item
        return out
    def nonfinite(value):
        raise ValueError('Nonfinite saved output number')
    try:
        result=json.loads(raw,object_pairs_hook=pairs,parse_constant=nonfinite)
        population=0
        stack=[(result,0)]
        while stack:
            item,depth=stack.pop();population+=1
            need(depth<=64 and population<=1_000_000,'Saved output JSON complexity exceeded')
            if type(item) is dict:
                need(all(type(k) is str and len(k)<=256 for k in item),'Invalid saved output key')
                stack.extend((v,depth+1) for v in item.values())
            elif type(item) is list:
                stack.extend((v,depth+1) for v in item)
            elif type(item) is float:
                need(math.isfinite(item),'Nonfinite saved output number')
            elif type(item) is int:
                need(abs(item)<=2**63-1,'Saved output integer exceeded')
            elif type(item) is str:
                need(len(item)<=1024*1024,'Saved output string exceeded')
            else:
                need(item is None or type(item) is bool,'Invalid saved output value')
        return result
    except (UnicodeError,RecursionError,OverflowError) as exc:
        raise ValueError('Invalid saved output JSON') from exc


def value(root, name):
    if name in {'state.json','result.json'}:
        return decode_output_json(read(root,name,MAX_OUTPUT_JSON))
    return parse_json(read(root,name,2*1024*1024))


def closed_inventory(out):
    """Only the fixed root + inputs tree; reject unknown names before descent."""
    from .server.real_daily_source_handles import _directory_chain
    directory=None;inputs=None;reopened=None
    root_names={'inputs','manifest.json','candidate.json','factor.csv','result.json','report.md','state.json'}
    def scan(fd,names,folder=None):
        seen=set()
        with os.scandir(fd) as entries:
            for entry in entries:
                need(entry.name in names and entry.name not in seen,'Unexpected main output entry')
                seen.add(entry.name)
                info=os.stat(entry.name,dir_fd=fd,follow_symlinks=False)
                if entry.name==folder:need(stat.S_ISDIR(info.st_mode),'Main inputs must be a directory')
                else:need(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'Main output must be a single-link regular file')
        need(seen==names,'Exact main output inventory required')
    try:
        directory,chain=_directory_chain(Path(out).absolute(),())
        scan(directory,root_names,'inputs')
        inputs=os.open('inputs',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
        before=os.fstat(inputs)
        scan(inputs,set(INPUTS.values()))
        after=os.stat('inputs',dir_fd=directory,follow_symlinks=False)
        need((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino),'Main inputs directory changed')
        reopened,current_chain=_directory_chain(Path(out).absolute(),())
        need(current_chain==chain,'Main output directory chain changed')
        current_inputs=os.stat('inputs',dir_fd=reopened,follow_symlinks=False)
        need((before.st_dev,before.st_ino)==(current_inputs.st_dev,current_inputs.st_ino),'Main inputs locator changed')
    except OSError as exc:
        raise ValueError('Main output inventory unavailable') from exc
    finally:
        for fd in (inputs,reopened,directory):
            if fd is not None:os.close(fd)


def code_identity():
    root=Path(__file__).resolve().parents[1]
    files={name:hashlib.sha256(read(root,name)).hexdigest() for name in CODE_FILES}
    return {'files':files,'digest':digest(files)}


def assessment_snapshot(source):
    assessment=source['assessment'];reg=source['registration']
    return {'schema_version':1,'assessment_id':reg['assessment_id'],'assessment_digest':reg['assessment_digest'],
        **{key:deepcopy(assessment[key]) for key in ('selection','method','method_digest','event_review',
            'source_facts','origin','source_authenticated','real_source_ready','point_in_time','final_numeric_access_allowed')},
        'selection_digest':digest(assessment['selection'])}


def validate_inputs(base, descriptor):
    from .real_daily_main_task import parse_main_task
    need(type(descriptor) is dict and set(descriptor)==DESCRIPTOR_KEYS and
         type(descriptor['schema_version']) is int and descriptor['schema_version']==3 and
         descriptor['job_version']==RUN_VERSION and descriptor['inputs']==INPUTS,'Closed main worker descriptor required')
    reg=value(base,INPUTS['registration']);assessment=value(base,INPUTS['assessment'])
    need(reg['registration_digest']==descriptor['registration_digest']==digest({k:v for k,v in reg.items() if k!='registration_digest'}), 'Registration snapshot changed')
    task=parse_main_task(descriptor['task'],main_execution_binding(reg),reg['selection'],reg['catalog'])
    need(task==value(base,INPUTS['task']) and value(base,INPUTS['catalog'])==reg['catalog'],'Frozen task/catalog changed')
    need(assessment['assessment_digest']==reg['assessment_digest'] and assessment['selection']==reg['selection'] and
         assessment['method']==reg['method'] and assessment['method_digest']==reg['method_digest'] and
         assessment['selection_digest']==reg['selection_digest'] and assessment['final_numeric_access_allowed'] is False,
         'Assessment snapshot changed')
    need('input_pins' not in assessment and 'raw' not in descriptor and 'db_path' not in descriptor,'Private source locator refused')
    prefix=read(base,INPUTS['prefix'])
    need(hashlib.sha256(prefix).hexdigest()==reg['prefix_sha256'],'Development prefix changed')
    receipt_raw=read(base,INPUTS['receipt']);receipt=parse_json(receipt_raw)
    need(hashlib.sha256(receipt_raw).hexdigest()==reg['receipt_sha256'] and
         receipt['prefix_sha256']==reg['prefix_sha256'] and receipt['development_content_digest']==reg['development_content_digest'], 'Prefix receipt changed')
    need(receipt['counters']['final_value_tokens_decoded']==0 and receipt['counters']['volume_value_tokens_decoded']==0,'Unsupported numeric scope')
    paper=value(base,INPUTS['paper']);pdf=read(base,INPUTS['paper_pdf'])
    need(hashlib.sha256(pdf).hexdigest()==descriptor['paper_sha256'] and digest(paper)==descriptor['paper_document_digest'],'Paper snapshot changed')
    verify_paper(paper,Path(base)/INPUTS['paper_pdf'])
    for evidence in task['evidence']:
        match_evidence(Evidence(**evidence),paper)
    return task,reg,prefix


def load_panels(prefix, registration):
    import pandas as pd
    fields=('open','high','low','close');selection=registration['selection']
    grid=[(day,asset) for day in selection['development_sessions'] for asset in selection['assets']]
    reader=csv.reader(io.StringIO(prefix.decode('utf-8'),newline=''))
    need(next(reader)==['date','asset',*fields],'Exact OHLC columns required')
    rows=[];observations=[]
    for i,row in enumerate(reader):
        need(i<len(grid) and len(row)==6 and row[:2]==list(grid[i]),'Prefix grid changed')
        numbers=[float(x) for x in row[2:]]
        need(all(math.isfinite(x) and x>0 for x in numbers),'Invalid OHLC numeric')
        o,h,l,c=numbers;need(l<=min(o,c)<=max(o,c)<=h,'Invalid OHLC range')
        rows.append(numbers);observations.append({'date':row[0],'asset':row[1],'values_hex':[x.hex() for x in numbers]})
    need(len(rows)==len(grid) and digest(observations)==registration['development_content_digest'] and digest([list(x) for x in grid])==registration['development_index_digest'],'Prefix content changed')
    width=len(selection['assets'])
    panels={field:pd.DataFrame([[row[j] for row in rows[i:i+width]] for i in range(0,len(rows),width)],
        index=pd.DatetimeIndex(selection['development_sessions'],name='date'),columns=selection['assets']) for j,field in enumerate(fields)}
    metadata={'version':registration['registration_version'],'data_kind':'real_daily_ohlc_development',
        'origin':registration['origin'],'calendar_dates':selection['development_sessions'],'universe':selection['assets'],
        'selection_digest':registration['selection_digest'],'method_digest':registration['method_digest'],
        'final_numeric_access_allowed':False,'source_authenticated':False,'point_in_time':'not_certified'}
    for panel in panels.values(): panel.attrs['market_metadata']=deepcopy(metadata)
    return panels


def markdown(result, scalar, baseline):
    body={'metrics':result['metrics'],'daily':result['daily'],'scalar':scalar,'constant_zero_baseline':baseline}
    return '# Alpha101 #101 开发期评估\n\n原价毛收益；分红、成本、滑点和借券费用均排除。最终期数值读取 0，PIT 与投资表现未认证。\n\n候选：`'+result['expression']+'`\n\n以下数字来自实际计算工具；累计图为收益算术和。\n\n```json\n'+json_text(body)+'```\n'


def run_main_task(task_path, out_dir, mode='fixed'):
    from .expressions import compute_expression
    from .real_daily_method import evaluate_ohlc, verify_scalar101
    import pandas as pd
    need(mode in {'fixed','normalized_fixed','agent'},'Unsupported execution mode')
    task_path=Path(task_path).absolute();base=task_path.parent
    descriptor=value(base,task_path.name);task,reg,prefix=validate_inputs(base,descriptor)
    out=Path(out_dir).absolute();need(not out.exists(),'Attempt output already exists')
    code=code_identity();started=time.monotonic();out.mkdir(parents=True,exist_ok=False)
    (out/'inputs').mkdir()
    for name in INPUTS.values(): (out/'inputs'/name).write_bytes(read(base,name))
    candidate=task['candidates'][0];atomic_json(out/'candidate.json',candidate)
    panels=load_panels(prefix,reg)
    factor=compute_expression(candidate['expression'],panels)
    result=evaluate_ohlc(factor,panels,selection=reg['selection'])
    result.update(expression=candidate['expression'],expression_diagnostics=deepcopy(factor.attrs.get('expression_diagnostics',{})))
    scalar=verify_scalar101(panels,selection=reg['selection'],result=result)
    baseline=evaluate_ohlc(pd.DataFrame(0.0,index=factor.index,columns=factor.columns),panels,selection=reg['selection'])
    need(scalar['status']=='verified' and baseline['status']=='not_evaluable' and baseline['metrics']['mean_rank_ic'] is None,'Scalar or constant baseline failed')
    need(code_identity()==code and time.monotonic()-started<=task['budget']['max_seconds'],'Source drift or time budget exceeded')
    validate_inputs(base,descriptor)
    factor.to_csv(out/'factor.csv',index_label='date',float_format='%.17g')
    atomic_json(out/'result.json',result);(out/'report.md').write_text(markdown(result,scalar,baseline),encoding='utf-8')
    record={**deepcopy(candidate),'status':result['status'],'result':result,'result_digest':digest(result),
        'attempts':[{'number':1,'expression':candidate['expression'],'status':result['status'],'tool_call':1}],
        'artifacts':{name:hashlib.sha256(read(out,name)).hexdigest() for name in ('factor.csv','result.json')}}
    state={'schema_version':1,'run_version':RUN_VERSION,'status':'completed','task':task,'mode':mode,
        'candidates':[record],'tool_calls':1,'elapsed_seconds':time.monotonic()-started,'events':[],
        'error':None,'evaluation_scope':'development_prefix_validation_only','final_numeric_rows_parsed':0,
        'scalar_verification':scalar,'constant_zero_baseline':baseline,'finished_at':datetime.now(timezone.utc).isoformat()}
    atomic_json(out/'state.json',state)
    files={name:hashlib.sha256(read(out,name)).hexdigest() for name in sorted(FILES)}
    manifest={'schema_version':3,'producer_kind':PRODUCER_KIND,'inventory_version':INVENTORY_VERSION,
        'run_version':RUN_VERSION,'run_id':descriptor['run_id'],'attempt_id':descriptor['attempt_id'],
        'revision_id':descriptor['revision_id'],'worker_id':descriptor['worker_id'],
        'paper_sha256':descriptor['paper_sha256'],'paper_document_digest':descriptor['paper_document_digest'],
        'task_digest':digest(task),'execution':task['execution'],'registration_digest':reg['registration_digest'],
        'state_digest':digest(state),'candidate_digest':digest(candidate),'result_digest':digest(result),
        'mode':mode,'code':code,'files':files,'snapshot_files':{k:v for k,v in files.items() if k.startswith('inputs/')},
        'final_numeric_rows_parsed':0,'evaluation_scope':'development_prefix_validation_only',
        'signature':{'code':code['files'],'inputs':{'paper_pdf':descriptor['paper_sha256']},'mode':mode}}
    atomic_json(out/'manifest.json',manifest)
    return state


def verify_main_run(out_dir, *, expected_task=None, expected_manifest_sha256=None, require_current_code=False, expected_files=None):
    out=Path(out_dir).absolute();manifest=value(out,'manifest.json');state=value(out,'state.json')
    need(manifest['schema_version']==3 and manifest['producer_kind']==PRODUCER_KIND and manifest['inventory_version']==INVENTORY_VERSION and manifest['run_version']==RUN_VERSION,'Unsupported main producer')
    need(set(manifest['files'])==FILES,'Exact main output inventory required')
    closed_inventory(out)
    for name,sha in manifest['files'].items():need(hashlib.sha256(read(out,name)).hexdigest()==sha,'Main output bytes changed')
    if expected_files is not None:need(manifest['files']==expected_files,'External artifact pins changed')
    manifest_sha=hashlib.sha256(read(out,'manifest.json')).hexdigest()
    if expected_manifest_sha256 is not None:need(manifest_sha==expected_manifest_sha256,'External manifest pin changed')
    task=value(out,'inputs/task.json');reg=value(out,'inputs/registration.json');candidate=value(out,'candidate.json');result=value(out,'result.json')
    need(state['status']=='completed' and state['task']==task and digest(state)==manifest['state_digest'] and digest(task)==manifest['task_digest'],'Main state/task changed')
    descriptor={'schema_version':3,'job_version':RUN_VERSION,'task':task,'inputs':INPUTS,
        'paper_id':'frozen_snapshot','paper_sha256':manifest['paper_sha256'],
        'paper_document_digest':manifest['paper_document_digest'],'run_id':manifest['run_id'],
        'attempt_id':manifest['attempt_id'],'worker_id':manifest['worker_id'],
        'revision_id':manifest['revision_id'],'registration_digest':manifest['registration_digest']}
    # Byte/source binding, never numeric re-evaluation or current raw lookup.
    validate_inputs(out/'inputs',descriptor)
    if expected_task is not None:need(task==expected_task,'Main output belongs to another task')
    need(task['execution']==manifest['execution']==main_execution_binding(reg) and reg['registration_digest']==manifest['registration_digest'],'Main registration binding changed')
    need(state['mode']==manifest['mode'] and candidate==task['candidates'][0] and digest(candidate)==manifest['candidate_digest'] and result['expression']==candidate['expression'] and digest(result)==manifest['result_digest'],'Executed candidate identity changed')
    need(len(state['candidates'])==1 and state['candidates'][0]['result']==result and state['candidates'][0]['result_digest']==digest(result),'Result differs from saved candidate')
    need(read(out,'report.md').decode('utf-8')==markdown(result,state['scalar_verification'],state['constant_zero_baseline']),'Report differs from actual metrics')
    need(result['final_numeric_rows_used']==0 and state['final_numeric_rows_parsed']==manifest['final_numeric_rows_parsed']==0 and manifest['evaluation_scope']=='development_prefix_validation_only','Final numeric scope changed')
    need(set(manifest['code']['files'])==set(CODE_FILES) and digest(manifest['code']['files'])==manifest['code']['digest'],'Code inventory changed')
    if require_current_code:need(code_identity()==manifest['code'],'Main code changed since computation')
    closed_inventory(out)
    return {'valid':True,'verified':True,'status':state['status'],'state_digest':digest(state),'task_digest':digest(task),
        'manifest_sha256':manifest_sha,'candidate_results':{candidate['id']:digest(result)},
        'final_numeric_rows_parsed':0,'evaluation_scope':'development_prefix_validation_only'}


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('task_path');parser.add_argument('--out',required=True);parser.add_argument('--mode',default='fixed')
    args=parser.parse_args();run_main_task(args.task_path,args.out,args.mode);return 0


if __name__=='__main__':sys.exit(main())
