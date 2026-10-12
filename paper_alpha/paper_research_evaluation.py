"""Aggregate predeclared paper-research cases; never run or judge a model here.

Automatic checks are host-produced observations. Their provenance is verified
by the execution harness before recording; this module preserves their scope.
"""
from collections import Counter
from copy import deepcopy
import math

from .semantic_materials import DIMENSIONS
from .storage import digest

CHECK_OUTCOMES = ('passed', 'failed', 'not_assessed', 'not_applicable', 'not_run')
USAGE_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'model_calls', 'cost', 'elapsed_seconds')


def _distinct(values, label):
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError(label + ' must be explicit identifiers')
    if len(values) != len(set(values)):
        raise ValueError('Duplicate ' + label)
    return values


def _usage(rows):
    result = {}
    for name in ('native_invocations', *USAGE_FIELDS):
        observed, unknown = [], []
        for row in rows:
            provider = row.get('provider') or {}
            value = provider.get(name) if name == 'native_invocations' else (provider.get('usage') or {}).get(name)
            if value is None:
                unknown.append(row['case_id'])
            elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Usage must be an observed nonnegative number or null: ' + name)
            else:
                observed.append(value)
        result[name] = {'observed_sum': sum(observed), 'total': None if unknown else sum(observed),
                        'coverage': {'observed_cases': len(observed), 'planned_cases': len(rows),
                                     'rate': len(observed) / len(rows) if rows else None},
                        'unknown_case_ids': unknown, 'status': 'unknown' if unknown else 'complete'}
    result['fixture_case_ids'] = [row['case_id'] for row in rows if (row.get('provider') or {}).get('fixture') is True]
    result['native_provider_case_ids'] = [row['case_id'] for row in rows
                                        if (row.get('provider') or {}).get('fixture') is False
                                        and ((row.get('provider') or {}).get('native_invocations') or 0) > 0]
    result['scope'] = 'Saved per-case observations; native invocation counts alone do not authenticate real-model inference.'
    return result


def _group(rows, automatic_dimensions, human_dimensions):
    automatic = {}
    for name in automatic_dimensions:
        counts = Counter(row['automatic_checks'][name]['outcome'] for row in rows)
        automatic[name] = {outcome: counts[outcome] for outcome in CHECK_OUTCOMES}
        automatic[name].update(denominator=len(rows),
                               planned_pass_rate=counts['passed'] / len(rows) if rows else None)
    human = {}
    for name in human_dimensions:
        declared = [row['case_id'] for row in rows if row['human_review'].get('source') == 'human'
                    and isinstance(row['human_review'].get('scores'), dict)
                    and row['human_review']['scores'].get(name) is not None]
        human[name] = {'declared_human_case_ids': declared,
                       'pending_case_ids': [row['case_id'] for row in rows if row['case_id'] not in declared],
                       'denominator': len(rows), 'score': None,
                       'status': 'human_declarations_available' if declared else 'pending_human'}
    return {'case_ids': [row['case_id'] for row in rows], 'planned_cases': len(rows),
            'recorded_cases': sum(row['record_present'] for row in rows),
            'status_counts': dict(sorted(Counter(row['status'] for row in rows).items())),
            'automatic_dimensions': automatic, 'human_dimensions': human, 'usage': _usage(rows)}


def summarize(protocol, records):
    """One row per frozen case, including missing, rejected and blocked work."""
    automatic = _distinct(protocol['scoring']['automatic_dimensions'], 'automatic dimension')
    human = _distinct(protocol['scoring']['human_dimensions'], 'human dimension')
    if set(automatic) & (set(human) | set(DIMENSIONS) | {'citation_support'}):
        raise ValueError('Human semantic dimensions cannot be automatic citation/engineering scores')
    declared = {}
    for case in protocol['cases']:
        if not isinstance(case['id'], str) or not case['id'] or case['id'] in declared:
            raise ValueError('Missing or duplicate case ID')
        if case['split'] not in ('development', 'evaluation'):
            raise ValueError('Unknown case split')
        declared[case['id']] = case
    for split in ('development', 'evaluation'):
        listed = _distinct(protocol[split + '_cases'], split + ' case ID')
        if set(listed) != {case['id'] for case in declared.values() if case['split'] == split}:
            raise ValueError('Case membership differs from its declared split: ' + split)
    indexed = {}
    for record in records:
        identity = record['case_id']
        if identity not in declared or identity in indexed:
            raise ValueError('Foreign or duplicate observed case ID')
        if record.get('split', declared[identity]['split']) != declared[identity]['split']:
            raise ValueError('Observed case split differs from the frozen protocol')
        if not isinstance(record.get('status'), str) or not record['status']:
            raise ValueError('An observed case requires its actual status')
        indexed[identity] = record
    rows = []
    for identity, case in declared.items():
        missing = identity not in indexed
        row = deepcopy(indexed.get(identity, {'case_id': identity, 'status': 'not_run'}))
        row.update(split=case['split'], protocol_case_digest=digest(case), record_present=not missing)
        checks = row.setdefault('automatic_checks', {})
        if set(checks) - set(automatic):
            raise ValueError('Observed check is outside the frozen scoring dimensions')
        for name in automatic:
            checks.setdefault(name, {'outcome': 'not_run' if row['status'] == 'not_run' else 'not_assessed',
                                     'reason': 'No execution record' if missing else 'No host check observation'})
            if checks[name].get('outcome') not in CHECK_OUTCOMES:
                raise ValueError('Unknown automatic check outcome')
        review = row.setdefault('human_review', {'status': 'pending_human', 'scores': None})
        review.setdefault('scores', None)
        review.setdefault('status', 'pending_human')
        if review['scores'] is not None and review.get('source') != 'human':
            raise ValueError('Human scores require an explicit human source; use proxy_evaluation for agent judgments')
        if review['scores'] is None and review['status'] != 'pending_human':
            raise ValueError('A human-review status cannot imply assessment without scores')
        rows.append(row)
    return {'suite_id': protocol['suite_id'], 'frozen_at': protocol['frozen_at'],
            'protocol_digest': digest(protocol), 'records': rows,
            'by_split': {split: _group([row for row in rows if row['split'] == split], automatic, human)
                         for split in ('development', 'evaluation')},
            'human_quality_score': None, 'scientific_score': None, 'human_time_saved': None,
            'scope': 'Automatic engineering facts only. Quote location does not establish citation support; semantic judgments and human timing require actual human records.'}
