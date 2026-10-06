"""Read-only material linkage and local bundle integrity, not semantic scoring.

The verifier uses installed validation code. Archived source files are reading
material and are never imported or executed. Offline hashes do not authenticate
the exporting server, its creation receipts, or a reviewer's declared identity.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlencode, urlsplit
import ipaddress

ROOT = Path(__file__).resolve().parents[1]

from paper_alpha import semantic_materials
from paper_alpha.storage import digest, json_text
from paper_alpha.server.claim_reviews import LIMITATIONS as REVIEW_LIMITATIONS
from paper_alpha.server.research_assessments import DIMENSIONS, parse_timestamp
from paper_alpha.server.research_claims import LIMITATIONS as CLAIM_LIMITATIONS, _resolve
from paper_alpha.server.review_materials_schema import ReviewMaterialsDetail
from paper_alpha.server.response_schemas import HealthResponse

MAX_JSON = 8 * 1024 * 1024
MAX_SOURCE = 16 * 1024 * 1024
MAX_BUNDLE = 48 * 1024 * 1024
AUTHOR_IDS = ('main', 'SetupDataA', 'SetupDataB', 'Table1', 'Table8A', 'Table8B')
IMPLEMENTATION_FILES = ('scripts/prepare_case_review.py', 'scripts/case_review_packet.py',
                        'paper_alpha/review_material_packet.py',
                        'paper_alpha/server/review_materials.py', 'paper_alpha/server/review_materials_schema.py')
LIMITATIONS = [
    'This packet preserves exact server projections and source bytes; it does not determine narrative truth or economic validity.',
    'Offline consistency cannot authenticate the server, its original creation receipts, a complete review history or a declared human identity.',
    'Source-file verification, stored tool metrics, independent numerical reference and a new computation are different evidence; this export performs no computation.',
    'Original claims are automation drafts; existing exact-target judgments remain declared judgments, not software-verified semantic truth.',
    'Human source, reviewer, confirmation time and five outcomes remain blank in the separate worksheet; preparation never writes a review.',
    'Alpha101 daily results are synthetic validation outputs; industry MOM is a retrospective project modification with revised portfolio data and different baseline net exposure.',
    'No original-stock-paper reproduction, cost-adjusted performance, unseen-final-test result, model execution, semantic quality score or manual efficiency gain is inferred.',
    'Archived author and exporter source files are for reading only; this bundle does not execute them or prove a complete replay environment.',
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decode(data, limit=MAX_JSON):
    require(isinstance(data, bytes) and len(data) <= limit, 'JSON exceeds its bounded byte limit')
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'Duplicate JSON key')
            value[key] = item
        return value
    def number(text):
        value = float(text)
        require(math.isfinite(value), 'Nonfinite JSON number')
        return value
    def reject(_):
        raise ValueError('Nonfinite JSON constant')
    return json.loads(data.decode('utf8'), object_pairs_hook=pairs, parse_float=number, parse_constant=reject)


def source_url(target, source_id):
    return '/api/review-materials/' + target['claims_id'] + '/sources/' + source_id + '?' + urlencode(
        {'claim_id': target['claim_id'], 'expected_target_digest': target['target_digest']})


def blank_judgments(packet):
    return {'schema_version': 1, 'target_digest': packet['target']['target_digest'],
            'source': None, 'reviewer': None, 'confirmed_at': None,
            'dimensions': {name: {'outcome': None, 'reason': None} for name in DIMENSIONS},
            'supersedes_id': None, 'expected_supersedes_digest': None,
            'submission': 'Blank reading worksheet only; submit personal judgments through the existing exact-target review form.'}


def _identity(value, prefix):
    body = {k: v for k, v in value.items() if k not in {'id', 'digest', 'created_at'}}
    require(digest(body) == value['digest'] and value['id'] == prefix + value['digest'], 'Stored content identity differs')


def _status(target, records):
    replaced = {r['supersedes_id'] for r in records if r['supersedes_id'] is not None}
    active = [r for r in records if r['id'] not in replaced]
    human = [r for r in active if r['source'] == 'human']
    judgments = {tuple(r['dimensions'][name]['outcome'] for name in DIMENSIONS) for r in human}
    if not human:
        state, unknown = 'pending', list(DIMENSIONS)
    elif len(judgments) > 1:
        state = 'conflicting'
        unknown = [name for name in DIMENSIONS if len({r['dimensions'][name]['outcome'] for r in human}) > 1
                   or any(r['dimensions'][name]['outcome'] in {'not_assessed', 'not_applicable'} for r in human)]
    else:
        state = human[0]['declared_semantic_status']
        unknown = [name for name in DIMENSIONS if human[0]['dimensions'][name]['outcome'] in {'not_assessed', 'not_applicable'}]
    return {'schema_version': 1, 'target': target, 'records': len(records),
            'human_records': sum(r['source'] == 'human' for r in records),
            'automation_records': sum(r['source'] == 'automation' for r in records),
            'superseded_records': len(replaced), 'active_human_review_ids': [r['id'] for r in human],
            'active_automation_review_ids': [r['id'] for r in active if r['source'] == 'automation'],
            'human_declared_status': state, 'unknown_dimensions': unknown, 'semantic_quality_score': None,
            'original_result_approval': False, 'software_verified_semantic_truth': False, 'limitations': REVIEW_LIMITATIONS}


def verify_packet(packet, source_files=None):
    require(isinstance(packet, dict) and len(json_text(packet).encode('utf8')) <= MAX_JSON, 'Material packet exceeds 8 MiB')
    typed = ReviewMaterialsDetail.model_validate(packet).model_dump(exclude_unset=True)
    require(json_text(typed) == json_text(packet), 'Material projection differs from closed typed contract')
    require(packet['limitations'] == LIMITATIONS, 'Material limitations changed')
    require(digest({k: v for k, v in packet.items() if k != 'digest'}) == packet['digest'], 'Material digest differs')
    case, claims, target, records = (packet[k] for k in ('case', 'claims', 'target', 'reviews'))
    require(case['source_kind'] in {'daily_run', 'industry_mom_experiment'}, 'Unsupported material source domain')
    _identity(case, 'research_case_'); _identity(claims, 'research_claims_')
    context = case['context']
    require(case['source_digest'] == digest({k: case[k] for k in ('source_kind', 'source_id', 'context')}), 'Case source digest differs')
    require((claims['case_id'], claims['case_digest']) == (case['id'], case['digest']), 'Claims belong to another case')
    require(claims['limitations'] == CLAIM_LIMITATIONS, 'Claim limitations changed')
    results = {r['id']: r for r in context['results']}
    require(len(results) == len(context['results']), 'Duplicate result identity')
    expected_kind = 'daily_candidates' if case['source_kind'] == 'daily_run' else 'industry_mom_portfolio'
    for result in results.values():
        require(result['kind'] == expected_kind and digest(decode(result['payload_json'].encode())) == result['digest'], 'Result kind or payload digest differs')
    evidence = {e['id']: e for e in context['evidence']}
    require(len(evidence) == len(context['evidence']), 'Duplicate evidence identity')
    resolved = []
    require(len({c['id'] for c in claims['submitted_claims']}) == len(claims['submitted_claims']), 'Duplicate submitted claim identity')
    for draft in claims['submitted_claims']:
        require(set(draft['evidence_ids']) <= set(evidence), 'Claim cites foreign evidence')
        origins = {evidence[e]['origin'] for e in draft['evidence_ids']}
        if draft['kind'] == 'evidence_statement':
            expected_origin = {'paper_original': 'paper', 'author_code': 'author_code'}.get(draft['attribution'])
            require(expected_origin is not None and origins == {expected_origin}, 'Claim attribution conflicts with evidence')
        elif draft['kind'] == 'interpretation':
            require(draft['attribution'] in {'model_conjecture', 'user_modification', 'project_convention', 'unresolved'}, 'Interpretation promoted to paper truth')
        elif draft['kind'] == 'project_rule':
            require(draft['attribution'] in {'project_convention', 'user_modification'} and
                    (draft['attribution'] != 'project_convention' or not origins - {'project'}), 'Project rule attribution conflicts')
        else:
            require(draft['attribution'] == 'project_convention', 'Metric attributed to paper conclusion')
        metrics = []
        for reference in draft['metric_references']:
            result = results.get(reference['result_id'])
            require((reference['case_id'], reference['case_digest']) == (case['id'], case['digest']) and result is not None
                    and result['digest'] == reference['result_digest'], 'Foreign metric reference')
            metrics.append(_resolve(result, reference))
        resolved.append({k: draft[k] for k in ('id', 'kind', 'attribution', 'evidence_ids')} |
                        {'narrative_text': draft['text'], 'evidence_verifications': [evidence[e]['verification'] for e in draft['evidence_ids']],
                         'metrics': metrics, 'authoritative_display': '\n'.join(m['display'] for m in metrics) if metrics else None,
                         'citation_integrity': 'verified' if draft['evidence_ids'] else 'not_applicable',
                         'semantic_fidelity': 'unverified', 'status': 'draft', 'actor': 'automation'})
    require(resolved == claims['claims'], 'Resolved tool values or claim text differ from original submission')
    claim = next((c for c in resolved if c['id'] == target['claim_id']), None)
    require(claim is not None, 'Target claim absent')
    body = {'schema_version': 1, 'claims_id': claims['id'], 'claims_digest': claims['digest'],
            'claim_id': claim['id'], 'claim_digest': digest(claim), 'case_id': case['id'], 'case_digest': case['digest'],
            **{k: case[k] for k in ('source_kind', 'source_id', 'source_digest')}, 'case_context_digest': digest(context),
            'claim': claim, 'evidence': context['evidence'], 'definitions': context['definitions'],
            'results': [{k: r[k] for k in ('id', 'kind', 'digest', 'summary')} for r in context['results']],
            'provenance': context['provenance'], **{k: context[k] for k in ('data_scope', 'method_scope', 'stop_reason')},
            'case_state': context['state'], 'case_limitations': context['limitations'],
            'original_claim_limitations': claims['limitations'], 'limitations': REVIEW_LIMITATIONS}
    require(target == body | {'target_digest': digest(body)}, 'Exact target projection differs')
    require(records == sorted(records, key=lambda r: (r['created_at'], r['id'])), 'Review history order differs')
    by_id = {r['id']: r for r in records}
    require(len(by_id) == len(records), 'Duplicate review identity')
    scopes, children = set(), set()
    for record in records:
        _identity(record, 'claim_review_')
        require(record['target'] == target and record['limitations'] == REVIEW_LIMITATIONS, 'Review belongs to another exact target')
        outcomes = [record['dimensions'][name]['outcome'] for name in DIMENSIONS]
        semantic = 'failed' if 'failed' in outcomes else 'passed' if all(x == 'passed' for x in outcomes) else 'incomplete'
        require(record['declared_semantic_status'] == semantic, 'Declared review state differs')
        scope = (record['source'], record['reviewer'])
        if record['supersedes_id'] is None:
            require(record['supersedes_digest'] is None and scope not in scopes, 'Repeated root review scope')
            scopes.add(scope)
        else:
            parent = by_id.get(record['supersedes_id'])
            require(parent is not None and parent['id'] not in children and parent['id'] != record['id'], 'Missing or branching review ancestor')
            require((parent['source'], parent['reviewer']) == scope and parent['digest'] == record['supersedes_digest']
                    and parse_timestamp(parent['confirmed_at']) <= parse_timestamp(record['confirmed_at']), 'Review replacement scope/digest/time differs')
            children.add(parent['id'])
        visited, cursor = set(), record
        while True:
            require(cursor['id'] not in visited and len(visited) < 100, 'Review chain cycles or exceeds its bound')
            visited.add(cursor['id'])
            if cursor['supersedes_id'] is None:
                break
            require(cursor['supersedes_id'] in by_id, 'Missing review chain ancestor')
            cursor = by_id[cursor['supersedes_id']]
    require(packet['review_status'] == _status(target, records), 'Review status or active/human history coverage differs')
    sources = {s['id']: s for s in packet['sources']}
    require(len(sources) == len(packet['sources']), 'Duplicate source identity')
    provenance = {p['label']: p['value'] for p in context['provenance']}
    if case['source_kind'] == 'daily_run':
        require(set(sources) == {'alpha101-paper'}, 'Daily source whitelist differs')
        expected_pdf = semantic_materials.SOURCE_FILES['alpha101_paper'][1]
        require(sources['alpha101-paper']['sha256'] == expected_pdf, 'Daily paper is not fixed Alpha101')
        require(all('PDF SHA256 ' + expected_pdf in e['locator'] for e in evidence.values() if e['origin'] == 'paper'), 'Daily citation document differs')
    else:
        require(set(sources) == {'industry-paper', 'industry-method', 'industry-config', *('industry-author-' + x for x in AUTHOR_IDS)}, 'Industry source whitelist differs')
        require(sources['industry-paper']['sha256'] == provenance.get('paper_pdf_sha256'), 'Industry paper differs from context')
        for name in AUTHOR_IDS:
            e = evidence.get('industry-author-' + name)
            require(e is not None and 'SHA256 ' + sources['industry-author-' + name]['sha256'] in e['locator'], 'Author source differs from context')
    for source in sources.values():
        require(source['download_url'] == source_url(target, source['id']) and set(source['evidence_ids']) <= set(evidence), 'Source route/evidence membership differs')
        identity = source['id']
        if identity in {'alpha101-paper', 'industry-paper'}:
            filename = 'alpha101-paper.pdf' if identity == 'alpha101-paper' else 'momentum-paper.pdf'
            media_type, evidence_ids = 'application/pdf', [e['id'] for e in evidence.values() if e['origin'] == 'paper']
        elif identity in {'industry-method', 'industry-config'}:
            filename, media_type = identity.removeprefix('industry-') + '.json', 'application/json'
            evidence_ids = list(evidence) if identity == 'industry-method' else ['industry-project-method']
        else:
            filename, media_type, evidence_ids = identity.removeprefix('industry-author-') + '.m', 'text/plain', [identity]
        require((source['filename'], source['media_type'], source['evidence_ids']) == (filename, media_type, evidence_ids), 'Source filename/media/evidence projection differs')
    if source_files is not None:
        require(isinstance(source_files, dict) and set(source_files) == set(sources), 'Source byte inventory differs')
        require(sum(len(v) for v in source_files.values()) <= MAX_BUNDLE, 'Source bytes exceed bundle bound')
        for identity, value in source_files.items():
            source = sources[identity]
            require(isinstance(value, bytes) and len(value) == source['size'] and hashlib.sha256(value).hexdigest() == source['sha256'], 'Source bytes differ from frozen descriptor')
        if case['source_kind'] == 'industry_mom_experiment':
            from paper_alpha import mom_only, mom_only_workflow
            method = decode(source_files['industry-method']); config = decode(source_files['industry-config'])
            mom_only.validate_config(config)
            mom_only_workflow._validate_method(method, config, provenance['archive_sha256'])
            require(method['contract_digest'] == provenance['method_digest'] and digest(config) == provenance['config_digest'], 'Source method/config differs from original result')
    return {'passed': True, 'scope': 'local_snapshot_linkage_and_bytes_not_semantic_truth',
            'source_files_verified': source_files is not None, 'digest': packet['digest'],
            'target_digest': target['target_digest'], 'human_records': packet['review_status']['human_records'],
            'semantic_quality_score': None, 'llm_api_called': False}


def safe_read(path, limit=MAX_SOURCE):
    path = Path(path).absolute()
    require(path.resolve() == path, 'File path traverses a symbolic link')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= limit, 'File is linked, empty, nonregular or oversized')
        value = stream.read(limit + 1); after = os.fstat(stream.fileno())
        require(len(value) == before.st_size and len(value) <= limit and
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_nlink)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink), 'File changed during bounded read')
    return value


def render_report(packet):
    target = packet['target']
    lines = ['# 准确结论审核阅读材料', '', '本次材料不填写或提交人工判断。', '',
             'Case: ' + target['case_id'], 'Claims: ' + target['claims_id'], 'Claim: ' + target['claim_id'],
             'Target digest: ' + target['target_digest'], '', '## 先阅读这次准确文字', '',
             target['claim']['narrative_text'], '', '服务计算的实际值：', target['claim']['authoritative_display'] or '此项没有数值引用', '',
             '数据范围：' + target['data_scope'], '方法范围：' + target['method_scope'], '', '## 原始来源', '']
    for source in packet['sources']:
        lines.append('- [' + source['label'] + '](sources/' + source['id'] + '/' + source['filename'] + ')；SHA256 ' + source['sha256'])
    lines.extend(['', '## 判断顺序', '', '1. 打开原 PDF，按下面的页码与定位核对引文。',
                  '2. 阅读候选定义/项目规则和 packet.json 内实际结果，区分原文、修改与推测。',
                  '3. 对照服务派生数字及 JSON pointer，核对当前结论范围。',
                  '4. 由本人填写已有网页五维表单；不把本地空白 worksheet 当作已提交审核。', '', '## 本次证据', ''])
    lines.extend('- ' + e['id'] + '：' + e['locator'] + '\n\n' + e['text'] for e in target['evidence'])
    lines.extend(['', '## 当前审核状态', '', packet['review_status']['human_declared_status'],
                  '已有人工声明条数：' + str(packet['review_status']['human_records']), '', '## 范围限制', ''])
    lines.extend('- ' + value for value in packet['limitations'])
    return '\n'.join(lines) + '\n'


def load_bundle(path):
    root = Path(path).absolute()
    if root.is_file():
        require(root.name == 'packet.json', 'Select the bundle directory or its packet.json')
        root = root.parent
    require(root.resolve() == root and root.is_dir(), 'Bundle root is missing or linked')
    manifest = decode(safe_read(root / 'manifest.json', MAX_JSON))
    require(set(manifest) == {'schema_version', 'kind', 'materials_digest', 'target_digest', 'config', 'health', 'client_sha256', 'files', 'digest'}
            and type(manifest['schema_version']) is int and manifest['schema_version'] == 1 and manifest['kind'] == 'exact_claim_review_bundle', 'Invalid closed bundle manifest')
    require(digest({k: v for k, v in manifest.items() if k != 'digest'}) == manifest['digest'], 'Manifest digest differs')
    packet = decode(safe_read(root / 'packet.json', MAX_JSON))
    verify_packet(packet)
    expected = {'packet.json', 'report.md', 'blank-judgments.json', 'invocation.json', 'status.json'}
    source_files = {}
    for source in packet['sources']:
        relative = 'sources/' + source['id'] + '/' + source['filename']
        expected.add(relative); source_files[source['id']] = safe_read(root / relative)
    expected.update('verifier-source/' + name for name in IMPLEMENTATION_FILES)
    actual, total, visited = set(), 0, 0
    for p in root.rglob('*'):
        visited += 1
        require(visited <= 96, 'Bundle exceeds its directory-entry bound')
        info = p.lstat()
        require(not stat.S_ISLNK(info.st_mode), 'Bundle contains a link')
        if stat.S_ISDIR(info.st_mode):
            continue
        actual.add(str(p.relative_to(root)))
        total += info.st_size
        require(len(actual) <= 32 and total <= MAX_BUNDLE, 'Bundle exceeds bounded inventory')
    require(actual == expected | {'manifest.json'} and set(manifest['files']) == expected, 'Missing, extra or unregistered bundle file')
    captured = {}
    for name, entry in manifest['files'].items():
        require(isinstance(entry, dict) and set(entry) == {'sha256', 'size'}, 'Invalid file descriptor')
        require(isinstance(entry['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) and type(entry['size']) is int and entry['size'] > 0, 'Invalid file identity types')
        value = safe_read(root / name)
        require(entry == {'sha256': hashlib.sha256(value).hexdigest(), 'size': len(value)}, 'Bundle file hash/size differs')
        captured[name] = value
    require(manifest['client_sha256'] == {name: manifest['files']['verifier-source/' + name]['sha256'] for name in IMPLEMENTATION_FILES}, 'Verifier source inventory differs')
    verdict = verify_packet(packet, source_files)
    require(manifest['materials_digest'] == packet['digest'] and manifest['target_digest'] == packet['target']['target_digest'], 'Manifest target differs')
    require(decode(safe_read(root / 'blank-judgments.json', MAX_JSON)) == blank_judgments(packet), 'Worksheet contains changed/prefilled judgments')
    require(safe_read(root / 'report.md', MAX_JSON).decode('utf8') == render_report(packet), 'Report differs from actual packet values')
    config, health = manifest['config'], manifest['health']
    require(set(config) == {'base_url', 'workspace_id', 'claims_id', 'claim_id', 'expected_target_digest'}, 'Invalid export configuration')
    require(HealthResponse.model_validate(health).model_dump() == health, 'Health snapshot differs from closed contract')
    parts = urlsplit(config['base_url'])
    address = ipaddress.ip_address(parts.hostname or '')
    require(parts.scheme == 'http' and address.is_loopback and parts.port is not None and not parts.path
            and not parts.query and not parts.fragment and parts.username is None and parts.password is None,
            'Export origin is not an explicit numeric loopback HTTP origin')
    require((config['claims_id'], config['claim_id'], config['expected_target_digest']) ==
            (packet['target']['claims_id'], packet['target']['claim_id'], packet['target']['target_digest'])
            and config['workspace_id'] == health['workspace_id'] and health['ai_enabled'] is False
            and (health['version'], health['database_schema']) in {('0.21.0', 17), ('0.22.0', 17)}, 'Workspace/version/target export identity differs')
    invocation = decode(captured['invocation.json'])
    require(set(invocation) == {'schema_version', 'kind', 'request', 'business_writes', 'human_judgments_written'}
            and type(invocation['schema_version']) is int and invocation['schema_version'] == 1 and invocation['kind'] == 'exact_claim_review_export_invocation'
            and type(invocation['business_writes']) is int and invocation['business_writes'] == 0
            and type(invocation['human_judgments_written']) is int and invocation['human_judgments_written'] == 0,
            'Invalid immutable export invocation')
    request = invocation['request']
    require(isinstance(request, dict) and set(request) == {'base_url', 'workspace_id', 'claims_id', 'claim_id', 'expected_target_digest', 'timeout', 'get_retries'}, 'Invalid invocation request')
    require(all(request[k] == config[k] for k in ('base_url', 'workspace_id', 'claims_id', 'claim_id'))
            and request['expected_target_digest'] in (None, config['expected_target_digest'])
            and type(request['timeout']) in (int, float) and 1 <= request['timeout'] <= 30
            and type(request['get_retries']) is int and 0 <= request['get_retries'] <= 2, 'Invocation differs from completed export')
    status = decode(captured['status.json'])
    require(set(status) == {'schema_version', 'kind', 'passed', 'phase', 'materials_digest', 'target_digest', 'http_calls', 'business_writes', 'human_judgments_written'}
            and type(status['schema_version']) is int and status['schema_version'] == 1 and status['kind'] == 'exact_claim_review_export_status'
            and status['passed'] is True and status['phase'] == 'completed'
            and status['materials_digest'] == packet['digest'] and status['target_digest'] == packet['target']['target_digest']
            and type(status['http_calls']) is int and 1 <= status['http_calls'] <= 96
            and type(status['business_writes']) is int and status['business_writes'] == 0
            and type(status['human_judgments_written']) is int and status['human_judgments_written'] == 0,
            'Invalid completed export status')
    require(safe_read(root / 'manifest.json', MAX_JSON) == json_text(manifest).encode(), 'Manifest changed during bundle verification')
    for name, value in captured.items():
        require(safe_read(root / name) == value, 'Bundle bytes changed at final verification fence')
    final, visited = set(), 0
    for p in root.rglob('*'):
        visited += 1
        require(visited <= 96, 'Bundle exceeds its final directory-entry bound')
        info = p.lstat()
        require(not stat.S_ISLNK(info.st_mode), 'Bundle contains a link at final verification fence')
        if not stat.S_ISDIR(info.st_mode):
            final.add(str(p.relative_to(root)))
    require(final == actual, 'Bundle inventory changed during verification')
    return deepcopy(packet)
