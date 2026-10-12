"""Closed daily tasks and a single pure producer for immutable host bindings."""
from copy import deepcopy
import json
import re
from .contracts import Task, require
from .daily_contract import parse_contract
from .evaluation import validate_config
from .storage import digest

TASK_KIND = 'registered_daily_development_v2'
CORE_KEYS = frozenset({'evidence','hypotheses','candidates','evaluation','budget'})
EXECUTION_KEYS = frozenset({'kind','registration_id','dataset_id','registration_digest','declaration_digest','input_digest','development_content_digest','development_index_digest','config_digest','catalog_digest','source_digest'})

def is_daily_task(value):
    return isinstance(value, dict) and value.get('schema_version') == 2 and isinstance(value.get('execution'), dict) and value['execution'].get('kind') == TASK_KIND

def execution_binding(registration, dataset_id):
    from .server.provider_catalog import catalog_from_daily_contract
    row = dict(registration)
    contract = parse_contract(json.loads(row['contract_json']) if isinstance(row['contract_json'],str) else row['contract_json'])
    value = {'kind': TASK_KIND, 'registration_id':row['id'], 'dataset_id':dataset_id,
             **{k:row[k] for k in ('registration_digest','declaration_digest','input_digest','development_content_digest','development_index_digest')},
             'config_digest':digest(contract.to_dict()['evaluation']),
             'catalog_digest':catalog_from_daily_contract(contract)['catalog_digest']}
    value['source_digest'] = digest(value)
    validate_execution(value)
    return value

def validate_execution(value):
    require(type(value) is dict and set(value) == EXECUTION_KEYS, 'Closed daily execution binding required')
    require(value['kind'] == TASK_KIND, 'Unsupported daily execution kind')
    for key in ('registration_id','dataset_id'):
        require(type(value[key]) is str and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value[key])), 'Invalid execution identity: '+key)
    for key in EXECUTION_KEYS - {'kind','registration_id','dataset_id'}:
        require(type(value[key]) is str and bool(re.fullmatch(r'[0-9a-f]{64}',value[key])), 'Invalid execution digest: '+key)
    require(digest({k:v for k,v in value.items() if k != 'source_digest'}) == value['source_digest'], 'Execution source digest changed')
    return deepcopy(value)

def parse_daily_task(value, expected_binding, declared_calendar, catalog):
    require(type(value) is dict and set(value) == CORE_KEYS | {'schema_version','title','execution'}, 'Closed flat daily task required')
    require(type(value['schema_version']) is int and value['schema_version'] == 2, 'Daily task schema must be integer 2')
    require(type(value['title']) is str and 0 < len(value['title'].strip()) <= 200,'Invalid daily task title')
    actual = validate_execution(value['execution']); validate_execution(expected_binding)
    require(digest(actual) == digest(expected_binding),'Daily task host binding changed')
    require(digest(value['evaluation']) == actual['config_digest'],'Evaluation differs from registered configuration')
    config = validate_config(value['evaluation'],declared_calendar)
    require(catalog['catalog_digest'] == actual['catalog_digest'],'Catalog differs from host binding')
    budget = value['budget']
    require(type(budget) is dict, 'Budget must be an object')
    for key, item in budget.items():
        require(type(item) in (int,float) if key=='max_seconds' else type(item) is int, 'Budget values require explicit bounded numeric types')
    core = {k:deepcopy(value[k]) for k in CORE_KEYS}
    core['evaluation']=config
    parsed = Task.parse({**core,'schema_version':1,'title':value['title'],'paper':'paper.json','paper_pdf':'paper.pdf','data':'market.csv','data_metadata':'metadata.json'})
    require(len(parsed.candidates) <= parsed.budget.max_candidates,'Candidate count exceeds budget')
    require(all(len(c.expression) <= 2048 for c in parsed.candidates),'Expression exceeds 2048 characters')
    return {'schema_version':2,'title':value['title'],**{k:parsed.to_dict()[k] for k in CORE_KEYS},'execution':actual}
