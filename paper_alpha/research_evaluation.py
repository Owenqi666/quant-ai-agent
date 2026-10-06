"""Independent research samples: exact snapshots, declared references, no AI.

No database, experiment, human label, network request or saved code is executed.
The input is the read-only review-material export, whose local integrity is
checked separately from semantic fidelity and any future model evaluation.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import stat

import pydantic

from .research_evaluation_schema import ResearchEvaluation, ResearchSample
from .server.claim_reviews import LIMITATIONS as REVIEW_LIMITATIONS, _semantic_status
from .server.claim_reviews_schema import ClaimReviewTarget
from .server.research_assessments import DIMENSIONS, parse_timestamp
from .server.research_cases import CASE_FIELDS
from .server.research_claims import LIMITATIONS as CLAIM_LIMITATIONS, _resolve
from .storage import digest, json_text

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ('paper_alpha/research_evaluation.py', 'paper_alpha/research_evaluation_schema.py',
                'scripts/evaluate_research_sample.py', 'paper_alpha/review_material_packet.py',
                'paper_alpha/server/review_materials_schema.py', 'paper_alpha/server/claim_reviews.py',
                'paper_alpha/server/claim_reviews_schema.py', 'paper_alpha/server/research_assessments.py',
                'paper_alpha/server/research_cases.py', 'paper_alpha/server/research_cases_schema.py',
                'paper_alpha/server/research_claims.py', 'paper_alpha/server/research_claims_schema.py',
                'paper_alpha/author_panel_schema.py', 'paper_alpha/storage.py',
                'paper_alpha/mom_only.py', 'paper_alpha/mom_only_workflow.py')
MAX_JSON = 16 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_TREE = 96 * 1024 * 1024
MAX_FILES = 128
MAX_CHAIN = 100
LIMITATIONS = [
    'Technical checks concern exact frozen contracts and source bytes, not economic mechanism or faithful research prose.',
    'Result pointers resolve saved tool values; the source calculation and independent numeric reference are not rerun.',
    'Human source and reviewer are local declarations, not authenticated identities or externally certified ground truth.',
    'The material exporter checks its service records; offline checks do not prove database receipts, omitted history or trusted authorship.',
    'An entirely consistently rewritten local bundle cannot be authenticated by its own unkeyed hashes.',
    'Automation reviews do not supply human reference; unknown, conflicting and superseded judgments remain explicit.',
    'Declaration agreement compares existing local review declarations, never model output, model accuracy or objective semantic truth.',
    'Saved claim text remains automation/draft/unverified. No actual LLM execution or prediction is supplied in this version.',
    'The two supported development domains are not an independent final research test set or general efficiency benchmark.',
    'Daily Alpha101 uses declared development inputs; industry MOM is a retrospective gross-only project modification, not original-paper reproduction.',
    'Reserved periods are not evaluated and may not be promoted to a fully unseen blind test.',
]


class ResearchEvaluationError(ValueError):
    pass


def _bounded(value):
    if len(json_text(value).encode('utf8')) > MAX_JSON:
        raise ResearchEvaluationError('Research snapshot exceeds 16 MiB')
    return value


def _equal(actual, expected, message):
    if json_text(actual) != json_text(expected):
        raise ResearchEvaluationError(message)


def _body(value, metadata=('id', 'digest', 'created_at')):
    return {key: item for key, item in value.items() if key not in metadata}


def _packet_api():
    # Always use the installed library; archived verifier source is never run.
    from . import review_material_packet
    return review_material_packet


def _claims(case, claims):
    if claims['case_id'] != case['id'] or claims['case_digest'] != case['digest']:
        raise ResearchEvaluationError('Claim batch belongs to a different case')
    if claims['digest'] != digest(_body(claims)) or claims['id'] != 'research_claims_' + claims['digest']:
        raise ResearchEvaluationError('Claim batch content identity differs')
    _equal(claims['limitations'], CLAIM_LIMITATIONS, 'Claim limitations differ')
    if len({item['id'] for item in claims['submitted_claims']}) != len(claims['submitted_claims']):
        raise ResearchEvaluationError('Claim identities must be unique')
    evidence = {item['id']: item for item in case['context']['evidence']}
    results = {item['id']: item for item in case['context']['results']}
    if len(evidence) != len(case['context']['evidence']) or len(results) != len(case['context']['results']):
        raise ResearchEvaluationError('Evidence/result identities must be unique')
    resolved = []
    for claim in claims['submitted_claims']:
        if not set(claim['evidence_ids']) <= set(evidence):
            raise ResearchEvaluationError('Claim evidence is outside the exact case')
        origins = {evidence[key]['origin'] for key in claim['evidence_ids']}
        kind, attribution = claim['kind'], claim['attribution']
        if kind == 'evidence_statement' and (not origins or origins != {{'paper_original': 'paper', 'author_code': 'author_code'}.get(attribution)}):
            raise ResearchEvaluationError('Evidence statement attribution differs')
        if kind == 'interpretation' and attribution not in {'model_conjecture', 'user_modification', 'project_convention', 'unresolved'}:
            raise ResearchEvaluationError('Interpretation attribution differs')
        if kind == 'project_rule' and (attribution not in {'project_convention', 'user_modification'} or attribution == 'project_convention' and origins - {'project'}):
            raise ResearchEvaluationError('Project rule attribution differs')
        if kind == 'metric' and attribution != 'project_convention':
            raise ResearchEvaluationError('Computed metrics cannot be paper conclusions')
        metrics = []
        for reference in claim['metric_references']:
            if reference['case_id'] != case['id'] or reference['case_digest'] != case['digest']:
                raise ResearchEvaluationError('Metric belongs to another case')
            result = results.get(reference['result_id'])
            if result is None or reference['result_digest'] != result['digest']:
                raise ResearchEvaluationError('Metric result identity/digest differs')
            metrics.append(_resolve(result, reference))
        resolved.append({key: claim[key] for key in ('id', 'kind', 'attribution', 'evidence_ids')} | {
            'narrative_text': claim['text'], 'evidence_verifications': [evidence[key]['verification'] for key in claim['evidence_ids']],
            'metrics': metrics, 'authoritative_display': '\n'.join(item['display'] for item in metrics) if metrics else None,
            'citation_integrity': 'verified' if claim['evidence_ids'] else 'not_applicable',
            'semantic_fidelity': 'unverified', 'status': 'draft', 'actor': 'automation'})
    _equal(claims['claims'], resolved, 'Saved claims differ from server-style frozen-result resolution')


def _target(case, claims, target):
    claim = next((item for item in claims['claims'] if item['id'] == target['claim_id']), None)
    if claim is None:
        raise ResearchEvaluationError('Target claim is outside the exact batch')
    context = case['context']
    body = {'schema_version': 1, 'claims_id': claims['id'], 'claims_digest': claims['digest'],
        'claim_id': claim['id'], 'claim_digest': digest(claim), 'case_id': case['id'], 'case_digest': case['digest'],
        **{key: case[key] for key in ('source_kind', 'source_id', 'source_digest')},
        'case_context_digest': digest(context), 'claim': claim,
        **{key: context[key] for key in ('evidence', 'definitions', 'provenance', 'data_scope', 'method_scope', 'stop_reason')},
        'results': [{key: item[key] for key in ('id', 'kind', 'digest', 'summary')} for item in context['results']],
        'case_state': context['state'], 'case_limitations': context['limitations'],
        'original_claim_limitations': claims['limitations'], 'limitations': REVIEW_LIMITATIONS}
    expected = ClaimReviewTarget.model_validate(body | {'target_digest': digest(body)}).model_dump()
    _equal(target, expected, 'Exact claim target differs from case/batch/source projection')


def _reviews(packet):
    records, target = packet['reviews'], packet['target']
    by_id = {item['id']: item for item in records}
    if len(by_id) != len(records):
        raise ResearchEvaluationError('Review identities must be unique')
    replaced = set()
    for review in records:
        _equal(review['target'], target, 'Review refers to another exact target')
        if review['digest'] != digest(_body(review)) or review['id'] != 'claim_review_' + review['digest']:
            raise ResearchEvaluationError('Review content identity differs')
        if review['declared_semantic_status'] != _semantic_status(review['dimensions']) or review['limitations'] != REVIEW_LIMITATIONS:
            raise ResearchEvaluationError('Review semantic status or limitations differ')
        visited, current = set(), review
        while True:
            if current['id'] in visited or len(visited) >= MAX_CHAIN:
                raise ResearchEvaluationError('Review replacement chain is cyclic or exceeds 100 nodes')
            visited.add(current['id'])
            parent_id = current['supersedes_id']
            if parent_id is None:
                if current['supersedes_digest'] is not None:
                    raise ResearchEvaluationError('Unbound parent digest')
                break
            parent = by_id.get(parent_id)
            if (parent is None or current['supersedes_digest'] != parent['digest']
                    or (current['source'], current['reviewer']) != (parent['source'], parent['reviewer'])
                    or parse_timestamp(current['confirmed_at']) < parse_timestamp(parent['confirmed_at'])):
                raise ResearchEvaluationError('Review replacement source/reviewer/digest/time differs')
            current = parent
        if review['supersedes_id'] is not None:
            if review['supersedes_id'] in replaced:
                raise ResearchEvaluationError('Review replacement history contains a fork')
            replaced.add(review['supersedes_id'])
    active = sorted((item for item in records if item['id'] not in replaced), key=lambda item: (item['created_at'], item['id']))
    human = [item for item in active if item['source'] == 'human']
    judgments = {tuple(item['dimensions'][name]['outcome'] for name in DIMENSIONS) for item in human}
    if not human:
        status, unknown = 'pending', list(DIMENSIONS)
    elif len(judgments) > 1:
        status = 'conflicting'
        unknown = [name for name in DIMENSIONS if len({item['dimensions'][name]['outcome'] for item in human}) > 1
                   or any(item['dimensions'][name]['outcome'] in {'not_assessed', 'not_applicable'} for item in human)]
    else:
        status = human[0]['declared_semantic_status']
        unknown = [name for name in DIMENSIONS if human[0]['dimensions'][name]['outcome'] in {'not_assessed', 'not_applicable'}]
    expected = {'schema_version': 1, 'target': target, 'records': len(records),
        'human_records': sum(item['source'] == 'human' for item in records),
        'automation_records': sum(item['source'] == 'automation' for item in records),
        'superseded_records': len(replaced), 'active_human_review_ids': [item['id'] for item in human],
        'active_automation_review_ids': [item['id'] for item in active if item['source'] == 'automation'],
        'human_declared_status': status, 'unknown_dimensions': unknown, 'semantic_quality_score': None,
        'original_result_approval': False, 'software_verified_semantic_truth': False, 'limitations': REVIEW_LIMITATIONS}
    _equal(packet['review_status'], expected, 'Review status/active-reference coverage differs from complete exported history')
    return active, replaced


def _domain(case):
    results = case['context']['results']
    if len(results) != 1:
        raise ResearchEvaluationError('This version requires exactly one original result projection')
    result = results[0]
    payload = json.loads(result['payload_json'])
    if digest(payload) != result['digest']:
        raise ResearchEvaluationError('Frozen result content digest differs')
    provenance = {item['label']: item['value'] for item in case['context']['provenance']}
    if len(provenance) != len(case['context']['provenance']):
        raise ResearchEvaluationError('Provenance labels must be unique')
    if case['source_kind'] == 'daily_run' and result['kind'] == 'daily_candidates':
        candidates = [item for item in payload if item.get('id') == 'alpha101']
        if len(candidates) != 1 or candidates[0].get('status') != 'evaluated':
            raise ResearchEvaluationError('Daily sample requires a uniquely evaluated Alpha101 candidate')
        evaluation = json.loads(provenance['evaluation'])
        if result['id'] != provenance['attempt_id'] or set(evaluation['splits']) != {'train', 'validation', 'test'}:
            raise ResearchEvaluationError('Daily evaluation/attempt contract differs')
        if candidates[0]['result'].get('split') != 'validation' or candidates[0]['result'].get('config') != evaluation:
            raise ResearchEvaluationError('Daily Alpha101 result must use the declared validation configuration')
        return 'daily_alpha101'
    if case['source_kind'] == 'industry_mom_experiment' and result['kind'] == 'industry_mom_portfolio':
        from . import mom_only
        mom_only.validate_config(payload['config'])
        if (payload['data_kind'] != 'market_derived_portfolio_returns' or payload['asset_kind'] != 'industry_portfolio'
                or payload['research_scope'] != 'project_modification' or payload['reserved_evaluated'] is not False
                or payload['human_judgment'] is not None or result['id'] != provenance['attempt_id']
                or result['digest'] != provenance['result_digest'] or digest(payload['config']) != provenance['config_digest']
                or payload['config_digest'] != provenance['config_digest'] or payload['source_id'] != mom_only.SOURCE_ID
                or payload['study_id'] != mom_only.STUDY_ID or payload['semantics_version'] != mom_only.SEMANTICS_VERSION):
            raise ResearchEvaluationError('Industry domain/source/scope/attempt contract differs')
        return 'industry_mom'
    raise ResearchEvaluationError('Unsupported research domain; no monthly-fixture or author-study coercion')


def _validate_material(packet, source_files=None):
    _bounded(packet)
    _packet_api().verify_packet(packet, source_files=source_files)
    case, claims, target = packet['case'], packet['claims'], packet['target']
    if case['digest'] != digest({key: case[key] for key in CASE_FIELDS}) or case['id'] != 'research_case_' + case['digest']:
        raise ResearchEvaluationError('Case content identity differs')
    source_body = {key: case[key] for key in ('source_kind', 'source_id', 'context')}
    if case['source_digest'] != digest(source_body):
        raise ResearchEvaluationError('Case source/context identity differs')
    _claims(case, claims)
    _target(case, claims, target)
    active, replaced = _reviews(packet)
    domain = _domain(case)
    if domain == 'daily_alpha101':
        payload = json.loads(case['context']['results'][0]['payload_json'])
        for metric in target['claim']['metrics']:
            if payload[int(metric['pointer'].split('/')[1])]['id'] != 'alpha101':
                raise ResearchEvaluationError('Daily Alpha101 target metric belongs to a different candidate')
    return domain, active, replaced


def _implementation():
    return {'engine': 'research-contract-evaluation-v1',
            'source_sha256': {name: hashlib.sha256(_read(ROOT / name)).hexdigest() for name in SOURCE_FILES},
            'python_version': platform.python_version(), 'pydantic_version': pydantic.__version__}


def build_sample(packet, *, source_files=None):
    """Freeze one exact development claim. No sources means no byte-proof claim."""
    try:
        _validate_material(packet, source_files)
        body = {'schema_version': 1, 'kind': 'research_evaluation_sample',
                'scope': 'exact_development_case_and_claim', 'material': deepcopy(packet),
                'implementation': _implementation(), 'model_execution': 'model_not_run'}
        fingerprint = digest(body)
        return ResearchSample.model_validate(_bounded(body | {'id': 'research_sample_' + fingerprint, 'digest': fingerprint})).model_dump(exclude_unset=True)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as exc:
        raise ResearchEvaluationError('Invalid research sample input: ' + str(exc)[:1200]) from exc


def verify_sample(sample, *, source_files=None):
    try:
        normalized = ResearchSample.model_validate(_bounded(sample)).model_dump(exclude_unset=True)
        _equal(sample, normalized, 'Sample contract is not canonical')
        if sample['digest'] != digest(_body(sample, ('id', 'digest'))) or sample['id'] != 'research_sample_' + sample['digest']:
            raise ResearchEvaluationError('Research sample content identity differs')
        implementation = sample['implementation']
        if set(implementation['source_sha256']) != set(SOURCE_FILES) or any(not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value) for value in implementation['source_sha256'].values()):
            raise ResearchEvaluationError('Evaluation implementation inventory differs')
        domain, _, _ = _validate_material(sample['material'], source_files)
        return {'passed': True, 'domain': domain, 'sources_verified': source_files is not None,
                'model_execution': 'model_not_run', 'reproduction': 'not_rerun'}
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as exc:
        raise ResearchEvaluationError('Research sample failed integrity checks: ' + str(exc)[:1200]) from exc


def _reference(packet, active, replaced):
    records = packet['reviews']
    human = [item for item in active if item['source'] == 'human']
    automated = [item for item in active if item['source'] == 'automation']
    refs = lambda items: [{'id': item['id'], 'digest': item['digest']} for item in sorted(items, key=lambda item: item['id'])]
    dimensions = []
    comparison = []
    for name in DIMENSIONS:
        outcomes = sorted({item['dimensions'][name]['outcome'] for item in human})
        status = 'pending' if not outcomes else 'conflicting' if len(outcomes) > 1 else 'unknown' if outcomes[0] not in {'passed', 'failed'} else 'eligible'
        outcome = outcomes[0] if status == 'eligible' else None
        dimensions.append({'dimension': name, 'status': status, 'outcome': outcome,
                           'declared_outcomes': outcomes, 'references': refs(human)})
        predictions = sorted({item['dimensions'][name]['outcome'] for item in automated})
        predicted = predictions[0] if len(predictions) == 1 and predictions[0] in {'passed', 'failed'} else None
        comparable = outcome is not None and predicted is not None
        comparison.append({'dimension': name, 'reference_outcome': outcome, 'automation_declared_outcomes': predictions,
                           'comparable': comparable, 'matched': outcome == predicted if comparable else None,
                           'excluded_reason': None if comparable else 'human_reference_' + status if outcome is None else 'no_unambiguous_automation_declaration'})
    counts = {status: sum(item['status'] == status for item in dimensions) for status in ('eligible', 'pending', 'unknown', 'conflicting')}
    status = 'pending' if not human else 'conflicting' if counts['conflicting'] else 'unknown' if counts['unknown'] else 'available'
    reference = {'scope': 'exact_target_local_human_declarations_not_authenticated', 'status': status,
        'total_records': len(records), 'human_records': sum(item['source'] == 'human' for item in records),
        'automation_records': sum(item['source'] == 'automation' for item in records),
        'active_human': refs(human), 'active_automation': refs(automated),
        'superseded': refs([item for item in records if item['id'] in replaced]), 'dimensions': dimensions,
        **{name + '_dimensions': value for name, value in counts.items()}}
    comparable = sum(item['comparable'] for item in comparison)
    matched = sum(item['matched'] is True for item in comparison)
    prediction_exists = any(item['automation_declared_outcomes'] and set(item['automation_declared_outcomes']) & {'passed', 'failed'} for item in comparison)
    agreement = {'metric': 'local_declaration_agreement_not_model_accuracy',
        'status': 'available' if comparable else 'no_comparable_dimensions' if prediction_exists else 'not_evaluated_no_prediction',
        'comparable_dimensions': comparable, 'matched_dimensions': matched,
        'declaration_agreement_rate': matched / comparable if comparable else None, 'details': comparison}
    return reference, agreement


def evaluate_sample(sample, *, source_files=None):
    """Resolve tool metrics and local references without rerunning calculation."""
    checked = verify_sample(sample, source_files=source_files)
    packet = sample['material']
    active, replaced = _reviews(packet)
    reference, comparison = _reference(packet, active, replaced)
    checks = [{'name': name, 'status': 'passed', 'detail': detail} for name, detail in (
        ('canonical_snapshot_identity', 'Material, Case, Claims and sample content digests match.'),
        ('exact_target_projection', 'Case/batch/claim/source/result/provenance agree with the exact target.'),
        ('supported_domain', 'Explicit ' + checked['domain'] + ' development-domain adapter; no coercion.'),
        ('evidence_and_attribution', 'Cited evidence belongs to this Case; attribution agrees with source origins. Prose remains unverified.'),
        ('frozen_numeric_references', 'All submitted metric pointers are resolved from the original result with the existing whitelist.'),
        ('review_history_and_status', 'Exact-target review identities, replacement chains and full active-reference/status projection match.'))]
    checks.append({'name': 'source_file_bytes', 'status': 'passed' if source_files is not None else 'not_checked',
                   'detail': 'Whitelisted source bytes match the material manifest.' if source_files is not None else 'No source bytes supplied; source file verification is incomplete.'})
    body = {'schema_version': 1, 'kind': 'research_sample_evaluation', 'sample_id': sample['id'],
        'sample_digest': sample['digest'], 'material_digest': packet['digest'], 'target_digest': packet['target']['target_digest'],
        'domain': checked['domain'], 'technical_status': 'passed' if source_files is not None else 'incomplete',
        'checks': checks, 'metrics': deepcopy(packet['target']['claim']['metrics']), 'human_reference': reference,
        'declaration_comparison': comparison, 'model_execution': 'model_not_run', 'semantic_quality_score': None,
        'model_accuracy': None, 'source_integrity_scope': 'source_bytes_and_exact_snapshot_consistency' if source_files is not None else 'source_bytes_not_checked',
        'numerical_reference_scope': 'frozen_result_pointer_values_only', 'reproduction': 'not_rerun',
        'reserved_evaluated': False, 'limitations': LIMITATIONS}
    return ResearchEvaluation.model_validate(_bounded(body | {'digest': digest(body)})).model_dump()


def report_markdown(report):
    report = ResearchEvaluation.model_validate(report).model_dump()
    if report['digest'] != digest(_body(report, ('digest',))):
        raise ResearchEvaluationError('Evaluation report digest differs')
    reference, comparison = report['human_reference'], report['declaration_comparison']
    lines = ['# Exact research sample evaluation', '',
        'Sample: ' + report['sample_id'], 'Sample digest: ' + report['sample_digest'],
        'Exact target: ' + report['target_digest'], 'Domain: ' + report['domain'],
        'Technical status: ' + report['technical_status'], 'Model: model_not_run',
        'Calculation reproduction: not_rerun; reserved_evaluated=false', '', '## Technical checks', '']
    lines += [f"- {item['name']}: {item['status']}. {item['detail']}" for item in report['checks']]
    lines += ['', '## Frozen tool values', '']
    lines += ['- ' + item['display'] for item in report['metrics']] or ['No numeric claim references in this target.']
    lines += ['', '## Exact human declaration reference', '',
        f"Status: {reference['status']}; human records={reference['human_records']}; automation records={reference['automation_records']}; eligible dimensions={reference['eligible_dimensions']}/5."]
    lines += [f"- {item['dimension']}: {item['status']}; declared outcomes=" + ', '.join(item['declared_outcomes']) for item in reference['dimensions']]
    lines += ['', '## Declaration comparison', '', comparison['metric'],
        f"Status: {comparison['status']}; comparable={comparison['comparable_dimensions']}; matched={comparison['matched_dimensions']}; agreement={comparison['declaration_agreement_rate']}",
        'Model accuracy=null; semantic quality=null.', '', '## Limits', '']
    lines += ['- ' + item for item in report['limitations']]
    return '\n'.join(lines) + '\n'


def _path(path):
    value = Path(path).absolute()
    if '..' in value.parts or value.resolve() != value:
        raise ResearchEvaluationError('Snapshot path contains traversal or a symbolic link')
    return value


def _read(path, maximum=MAX_FILE):
    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ResearchEvaluationError('Snapshot file must be bounded, regular and unshared')
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        fence = lambda info: (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_nlink)
        if len(data) > maximum or len(data) != before.st_size or fence(before) != fence(after):
            raise ResearchEvaluationError('Snapshot file changed during reading')
    return data


def _json(path):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ResearchEvaluationError('Duplicate JSON key')
            value[key] = item
        return value
    def constant(_):
        raise ResearchEvaluationError('Nonfinite JSON constant')
    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ResearchEvaluationError('Nonfinite JSON number')
        return parsed
    return json.loads(_read(path, MAX_JSON), object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def _inventory(root):
    root = _path(root)
    if not root.is_dir():
        raise ResearchEvaluationError('Snapshot directory is missing')
    result, total, visited = {}, 0, 0
    for path in sorted(root.rglob('*')):
        visited += 1
        if visited > MAX_FILES * 3:
            raise ResearchEvaluationError('Snapshot directory entry budget exceeded')
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        data = _read(path)
        total += len(data)
        if total > MAX_TREE or len(result) >= MAX_FILES:
            raise ResearchEvaluationError('Snapshot exceeds 128 files / 96 MiB')
        result[str(path.relative_to(root))] = hashlib.sha256(data).hexdigest()
    return result


def _sources(packet, root):
    return {item['id']: _read(root / 'sources' / item['id'] / item['filename']) for item in packet['sources']}


def _directory(path, *, create=False):
    """Open every absolute path component without following a directory link."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ResearchEvaluationError('Output directory contains traversal')
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _new_output(path):
    descriptor = _directory(path.parent, create=True)
    try:
        os.mkdir(path.name, mode=0o700, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_file(root, name, data):
    """Create only new files using anchored directories and O_EXCL/O_NOFOLLOW."""
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ResearchEvaluationError('Invalid immutable output name')
    if not isinstance(data, bytes) or len(data) > MAX_FILE:
        raise ResearchEvaluationError('Output bytes exceed their file bound')
    descriptor = _directory(root)
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        output = os.open(relative.parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=descriptor)
        with os.fdopen(output, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if _read(root / relative) != data:
        raise ResearchEvaluationError('Output bytes changed during publication')


def _write_json(root, name, value):
    _write_file(root, name, json_text(value).encode('utf8'))


def export_sample(packet_dir, out):
    """New-directory-only export. Failed inputs remain as an explicit artifact."""
    packet_dir = _path(packet_dir)
    if packet_dir.is_file():
        if packet_dir.name != 'packet.json':
            raise ResearchEvaluationError('Use a material bundle directory or its packet.json')
        packet_dir = packet_dir.parent
    out = _path(out)
    if out == packet_dir or out.is_relative_to(packet_dir) or packet_dir.is_relative_to(out):
        raise ResearchEvaluationError('Input and output must be separate, non-nested directories')
    _new_output(out)
    invocation = {'schema_version': 1, 'kind': 'research_evaluation_invocation',
                  'packet_dir': str(packet_dir), 'output_dir': str(out), 'model_execution': 'model_not_run', 'reserved_evaluated': False}
    _write_json(out, 'invocation.json', invocation)
    try:
        raw = _read(packet_dir / 'packet.json', MAX_JSON)
        _write_file(out, 'input-material-packet.json', raw)
        packet = _packet_api().load_bundle(packet_dir)
        before = _inventory(packet_dir)
        material = out / 'material'
        for name, expected in before.items():
            data = _read(packet_dir / name)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ResearchEvaluationError('Input material changed during copying')
            _write_file(out, 'material/' + name, data)
        if _inventory(packet_dir) != before or _inventory(material) != before:
            raise ResearchEvaluationError('Material copy inventory differs')
        _equal(_packet_api().load_bundle(material), packet, 'Material copy changed')
        source_files = _sources(packet, material)
        sample = build_sample(packet, source_files=source_files)
        report = evaluate_sample(sample, source_files=source_files)
        for name, expected in sample['implementation']['source_sha256'].items():
            data = _read(ROOT / name)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ResearchEvaluationError('Evaluation implementation changed during export')
            _write_file(out, 'source/' + name, data)
        _write_json(out, 'sample.json', sample)
        _write_json(out, 'report.json', report)
        _write_file(out, 'report.md', report_markdown(report).encode('utf8'))
        if _inventory(packet_dir) != before:
            raise ResearchEvaluationError('Input material changed before final export')
        body = {'schema_version': 1, 'kind': 'research_evaluation_bundle_manifest',
                'sample_digest': sample['digest'], 'report_digest': report['digest'],
                'invocation_digest': digest(invocation), 'files': _inventory(out)}
        _write_json(out, 'manifest.json', body | {'digest': digest(body)})
        return verify_bundle(out)
    except Exception as exc:
        try:
            _write_json(out, 'error.json', {'schema_version': 1, 'passed': False, 'kind': 'failed_research_sample_export',
                                         'error': type(exc).__name__ + ': ' + str(exc)[:2000], 'model_execution': 'model_not_run', 'reserved_evaluated': False})
        except (OSError, ResearchEvaluationError):
            pass  # Never follow changed output links just to retain a failure.
        raise ResearchEvaluationError('Research export failed; evidence retained at ' + str(out) + ': ' + str(exc)[:1200]) from exc


def verify_bundle(out):
    """Verify a relocated export; no source code or remote path is executed."""
    out = _path(out)
    manifest = _json(out / 'manifest.json')
    if set(manifest) != {'schema_version', 'kind', 'sample_digest', 'report_digest', 'invocation_digest', 'files', 'digest'} or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or manifest['kind'] != 'research_evaluation_bundle_manifest' or digest(_body(manifest, ('digest',))) != manifest['digest']:
        raise ResearchEvaluationError('Research bundle manifest differs')
    inventory = _inventory(out)
    if set(inventory) != set(manifest['files']) | {'manifest.json'} or any(inventory[name] != expected for name, expected in manifest['files'].items()):
        raise ResearchEvaluationError('Research bundle inventory differs')
    packet = _packet_api().load_bundle(out / 'material')
    expected_files = {'invocation.json', 'input-material-packet.json', 'sample.json', 'report.json', 'report.md'}
    expected_files.update('source/' + name for name in SOURCE_FILES)
    expected_files.update('material/' + name for name in _inventory(out / 'material'))
    if set(manifest['files']) != expected_files:
        raise ResearchEvaluationError('Research bundle contains an unexpected or missing file')
    invocation = _json(out / 'invocation.json')
    if (set(invocation) != {'schema_version', 'kind', 'packet_dir', 'output_dir', 'model_execution', 'reserved_evaluated'}
            or type(invocation['schema_version']) is not int or invocation['schema_version'] != 1
            or invocation['kind'] != 'research_evaluation_invocation' or invocation['model_execution'] != 'model_not_run'
            or invocation['reserved_evaluated'] is not False or any(not isinstance(invocation[key], str)
            or not Path(invocation[key]).is_absolute() or '..' in Path(invocation[key]).parts for key in ('packet_dir', 'output_dir'))
            or invocation['packet_dir'] == invocation['output_dir'] or manifest['invocation_digest'] != digest(invocation)):
        raise ResearchEvaluationError('Original invocation contract/digest differs')
    sample, report = _json(out / 'sample.json'), _json(out / 'report.json')
    _equal(packet, sample['material'], 'Sample material differs from its source bundle')
    _equal(_json(out / 'input-material-packet.json'), packet, 'Original material request differs')
    checked = verify_sample(sample, source_files=_sources(packet, out / 'material'))
    expected_report = evaluate_sample(sample, source_files=_sources(packet, out / 'material'))
    _equal(report, expected_report, 'Report differs from exact sample evaluation')
    if report_markdown(report).encode('utf8') != _read(out / 'report.md'):
        raise ResearchEvaluationError('Markdown report differs from actual JSON report')
    if manifest['sample_digest'] != sample['digest'] or manifest['report_digest'] != report['digest']:
        raise ResearchEvaluationError('Bundle result identities differ')
    for name, expected in sample['implementation']['source_sha256'].items():
        if hashlib.sha256(_read(out / 'source' / name)).hexdigest() != expected:
            raise ResearchEvaluationError('Frozen evaluation implementation digest differs')
    if _inventory(out) != inventory:
        raise ResearchEvaluationError('Bundle changed during final verification')
    return {'passed': True, 'sample_id': sample['id'], 'sample_digest': sample['digest'],
            'report_digest': report['digest'], 'domain': checked['domain'], 'technical_status': report['technical_status'],
            'human_reference_status': report['human_reference']['status'], 'model_execution': 'model_not_run',
            'reproduction': 'not_rerun', 'reserved_evaluated': False}
