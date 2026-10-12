"""Literal review proof from a verified saved main attempt, never current inputs.

The caller first verifies the complete original producer/export inventory. This
projection does not change its state digest, run a factor or confer permission.
"""
from ..storage import digest
from ..real_daily_main_workflow import PRODUCER_KIND, INPUTS, decode_output_json
from .real_daily_source_handles import parse_json, read_managed
import json


def frozen_source_projection(store, connection, run, checked, *, original_state=None):
    from ..real_daily_main_task import is_main_task
    # Ordinary histories keep their existing decoder and resource contract.
    original = (json.loads(run['state']) if run['state'] is not None else None) if original_state is None else original_state
    if not original or not is_main_task(original.get('task')):
        return None
    original = decode_output_json(run['state'])

    def need(condition):
        if not condition:
            raise ValueError('Frozen review source differs from verified saved attempt')

    need(checked and checked.get('verified') is True and run['status'] == 'completed')
    need(digest(original) == checked['state_digest'])
    row = connection.execute('SELECT * FROM rdw_result_bindings WHERE attempt_id=?',
                             (run['attempt_id'],)).fetchone()
    need(row is not None)
    binding = parse_json(row['payload_json'])
    need(binding['producer_kind'] == PRODUCER_KIND and digest(binding) == row['producer_digest'])
    need(all(binding[key] == run[column] for key, column in
             (('run_id', 'id'), ('attempt_id', 'attempt_id'), ('revision_id', 'revision_id'))))
    need(binding['manifest_sha256'] == checked['manifest_sha256'])
    base = binding['output_relative']
    need(base == 'runs/' + run['id'] + '/attempts/' + run['attempt_id'] + '/output')

    def frozen_json(name):
        relative = 'inputs/' + INPUTS[name]
        raw, _ = read_managed(store.root, base + '/' + relative,
                              maximum=2 * 1024 * 1024,
                              expected_sha256=binding['files'][relative])
        return parse_json(raw.decode('utf-8'))

    paper, task = frozen_json('paper'), frozen_json('task')
    need(digest(paper) == binding['paper_document_digest'])
    need(task == original['task'] and digest(task) == binding['task_digest'] == checked['task_digest'])
    need(binding['paper_sha256'] == binding['files']['inputs/' + INPUTS['paper_pdf']])
    need(isinstance(paper.get('id'), str) and bool(paper['id'].strip()))
    evidence = task['evidence']
    need(len({e['id'] for e in evidence}) == len(evidence))
    # verify_saved_main_result -> verify_main_run -> validate_inputs has already
    # checked every literal quote against this exact frozen paper/PDF identity.
    return {'schema_version': 1, 'run_id': run['id'], 'revision_id': run['revision_id'],
            'attempt_id': run['attempt_id'], 'state_digest': checked['state_digest'],
            'paper_id': paper['id'], 'document_sha256': binding['paper_sha256'],
            'evidence': [{'id': e['id'], 'page': e['page'], 'quote': e['quote'],
                          'citation_verified': True} for e in evidence]}
