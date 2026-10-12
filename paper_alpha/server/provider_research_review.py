"""Read-only, evidence-bound material for a human to review a provider draft.

An exported packet is a template, not a saved judgment or an approval. Result
hashing and the independent numerical oracle are deliberately separate checks.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..contracts import Evidence
from ..evidence import match_evidence, sha256, verify_paper
from ..storage import digest as storage_digest, json_text, read_json
from .db import connect
from .provider_contract import digest
from .proposal_requests import ProposalRequests
from .regression import verify_candidate
from .research_assessments import DIMENSIONS
from .research_jobs import ResearchJobs
from .service import ServiceError


class ResearchReviewError(ServiceError):
    def __init__(self, message, *, code='REVIEW_SOURCE_INTEGRITY', status=409):
        super().__init__(message, status)
        self.code = code


def _source(store, proposal):
    request, context = proposal['request'], proposal['context']
    try:
        research = store._fetch('researches', request['research_id'])
        paper = store._fetch('papers', research['paper_id'])
        dataset = store._fetch('datasets', research['dataset_id'])
        for filename in (paper['pdf_path'], dataset['data_path'], dataset['metadata_path']):
            path = Path(filename)
            store._safe_artifact_path(path, store.root)
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ResearchReviewError('Frozen source is not a regular file', code='SOURCE_INTEGRITY')
        source = ResearchJobs(store)._source(request['research_id'], request['base_revision_id'])
        if (storage_digest(source) != context['source_digest']
                or context['source_digest'] != request['expected_source_digest']
                or digest(context) != proposal['context_digest']
                or source['paper_sha256'] != context['paper_sha256']
                or source['data_file_sha256'] != context['data_sha256']):
            raise ResearchReviewError('Frozen proposal sources no longer match their identities')
        document = json.loads(paper['document'])
        verify_paper(document, paper['pdf_path'])
        return source, paper, document
    except ServiceError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ResearchReviewError('Frozen research sources are unavailable or changed', code='SOURCE_INTEGRITY') from exc


def _evidence(items, paper, *, required=False):
    result = []
    for item in items:
        try:
            checked = match_evidence(Evidence(**item), paper)
        except (ValueError, TypeError) as exc:
            if required:
                raise ResearchReviewError('Input evidence is not present in the original PDF') from exc
            checked = {**deepcopy(item), 'paper_id': paper['id'],
                       'document_sha256': paper['document_sha256'], 'citation_verified': False,
                       'semantic_fidelity': 'requires_human_review', 'error': 'QUOTE_OR_PAGE_INVALID'}
        result.append(checked)
    return result


def _response(service, proposal):
    if proposal['accepted_response_digest'] is not None:
        attempt = next(item for item in proposal['attempts'] if item['id'] == proposal['accepted_attempt_id'])
        output = service._json_path(service._folder(proposal['id']) / 'accepted' / 'response.json')
        expected = proposal['accepted_response_digest']
    else:
        attempt = next((item for item in reversed(proposal['attempts']) if item['response_digest']), None)
        if attempt is None:
            return None
        output = service._json_path(service._folder(proposal['id']) / 'attempts' / attempt['id'] / 'parsed.json')
        expected = attempt['response_digest']
    if (digest(output) != expected or output['proposal_id'] != proposal['id']
            or output['attempt_id'] != attempt['id'] or output['context_digest'] != proposal['context_digest']):
        raise ResearchReviewError('The response does not belong to this frozen request')
    return {'attempt_id': attempt['id'], 'response_digest': expected,
            'raw_sha256': attempt.get('raw_sha256'), 'model_output': output}


def _attempt(item):
    receipt = item.get('receipt')
    return {key: deepcopy(item.get(key)) for key in ('id', 'sequence', 'status', 'prepared_digest',
            'raw_sha256', 'response_digest', 'validation', 'error')} | {
        'receipt': None if receipt is None else {key: deepcopy(receipt.get(key)) for key in (
            'started_at', 'finished_at', 'elapsed_seconds', 'exit_code', 'requested_model', 'reported_model',
            'fixture', 'invocation_started', 'process_stopped', 'cli_version', 'response_sha256', 'usage')}}


def _computation(store, job):
    """Rehash the original run, then independently recompute supported numerics."""
    if not job.get('run_id'):
        return None
    with closing(connect(store.db_path)) as connection:
        connection.execute('BEGIN')
        row = store._one(connection, 'runs', job['run_id'])
        if row['research_id'] != job['research_id'] or row['revision_id'] != job['revision_id']:
            raise ResearchReviewError('Run identity differs from the handed-off research job')
        base = {'run_id': row['id'], 'attempt_id': row['attempt_id'], 'status': row['status'],
                'revision_id': row['revision_id'], 'integrity': 'not_verified', 'state_digest': None,
                'manifest_sha256': None, 'candidates': [], 'error': row.get('error')}
        recorded = json.loads(row['verification']) if row['verification'] else None
        if row['status'] not in {'completed', 'failed'} or not recorded or not recorded.get('verified'):
            base['missing_reason'] = 'A terminal verified experiment is not available; metrics are omitted.'
            return base
        rows = store._review_rows(connection, row['id'], row['attempt_id'])
        files = store._review_files(row['id'], row['attempt_id'])
        run, state = store._verified_state(connection, row['id'])
        output = store.root / 'runs' / run['id'] / 'attempts' / run['attempt_id'] / 'output'
        task = store._revision(store._one(connection, 'revisions', run['revision_id']))['task']
        candidates = []
        for candidate in state.get('candidates', []):
            actual = candidate.get('result')
            item = {key: deepcopy(candidate.get(key)) for key in (
                'id', 'hypothesis_id', 'expression', 'origin', 'changes', 'status', 'attempts', 'artifacts')}
            item['result'] = None if actual is None else {key: deepcopy(actual.get(key)) for key in (
                'schema_version', 'status', 'reason', 'split', 'expression', 'metrics', 'config',
                'execution', 'factor_diagnostics', 'expression_diagnostics', 'limitations')}
            item['independent_verification'] = (verify_candidate(output, candidate['id'], task)
                if candidate.get('status') == 'evaluated' else {
                    'outcome': 'not_comparable', 'checks': [], 'reason': 'Candidate was not evaluated.'})
            candidates.append(item)
        store._assert_review_snapshot(connection, rows, files)
        base.update(integrity='verified', state_digest=storage_digest(state),
                    manifest_sha256=sha256(output / 'manifest.json'), candidates=candidates)
        return base


def _job(store, proposal, response, identity):
    linked = proposal.get('research_job_id')
    if identity is not None and identity != linked:
        raise ResearchReviewError('Only the proposal\'s actual handed-off job can be included', code='REVIEW_TARGET_MISMATCH')
    if linked is None:
        return None, None
    job = ResearchJobs(store).get(linked)
    request = proposal['request']
    if (job['research_id'] != request['research_id'] or job['base_revision_id'] != request['base_revision_id']
            or job['source_digest'] != request['expected_source_digest'] or response is None
            or job['draft'] != response['model_output']['draft']):
        raise ResearchReviewError('Research job source or draft differs from the accepted response')
    saved_digest = None
    if job['result_json'] is not None:
        snapshot = json.loads(job['result_json'])
        saved_digest = storage_digest(snapshot)
        completed = [step for step in job['steps'] if step['action'] == 'complete' and step['output_json']]
        if not completed or json.loads(completed[-1]['output_json']).get('result_digest') != saved_digest:
            raise ResearchReviewError('Saved research job result snapshot differs from its ledger')
    summary = {key: deepcopy(job[key]) for key in ('id', 'digest', 'state', 'stop_reason', 'base_revision_id',
                 'revision_id', 'run_id', 'budget', 'candidate_checks', 'semantic_fidelity')}
    summary.update(saved_result_digest=saved_digest, steps=[{key: deepcopy(step.get(key)) for key in (
        'id', 'sequence', 'action', 'status', 'next_state', 'effect_key', 'error')} for step in job['steps']],
        handoff_note=proposal['handoff']['request'].get('note') if proposal.get('handoff') else None,
        execution_consent_is_human_quality_review=False)
    return summary, _computation(store, job)


def _block(value):
    text = json_text(value).rstrip() if not isinstance(value, str) else value
    longest = max((len(match.group()) for match in re.finditer(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}\n{text}\n{fence}'


def _markdown(packet):
    lines = ['# 研究草案审核材料', '',
        '人工审核：pending。以下机器检查不代表研究语义或市场效果获得认可；本文件不写入任何审批记录。', '',
        f"Proposal：{packet['proposal']['id']}，状态：{packet['proposal']['status']}。", '',
        '## 输出速览', '']
    for candidate in packet['candidates']:
        lines.extend([f"候选 {candidate['id']} 的实际提交表达式：", '', _block(candidate['expression']), ''])
    computation = packet['computation']
    if computation is not None:
        for candidate in computation['candidates']:
            actual = candidate['result']
            if actual is None:
                continue
            outcome = candidate['independent_verification']['outcome']
            lines.extend([f"{candidate['id']}：工具状态 {actual['status']}，区间 {actual['split']}；独立数值核对 {outcome}。", '',
                          '| 工具实际指标 | 原值 |', '| --- | --- |'])
            for name, value in (actual.get('metrics') or {}).items():
                lines.append(f"| {name} | {json.dumps(value, ensure_ascii=False, allow_nan=False)} |")
            lines.append('')
    if not packet['candidates']:
        lines.extend(['当前没有可解析的候选输出，详细失败或弃权原因见下方记录。', ''])
    if computation is None or not computation['candidates']:
        lines.extend(['当前没有可核对的计算指标；未设置默认零值。', ''])
    lines.extend(['## 论文与输入证据', '', f"论文：{packet['paper']['title']}（{packet['paper']['url']}）。", '',
        f"[打开原 PDF](<{packet['paper']['pdf_path']}>)", ''])
    for item in packet['input_evidence']:
        lines.extend([f"证据 {item['id']}，PDF 第 {item['page']} 页；引文字面核对：{item['citation_verified']}。", '',
                      _block(item['quote']), ''])
    lines.extend(['## 研究声明与机制归因', '', _block(packet['input_hypotheses']), '',
                  '## 实际模型输出', '', _block(packet['response']), '',
                  '模型提交引文的字面出处核对（语义仍需人工）：', '', _block(packet['output_evidence']), '',
                  '## 候选实现与机器检查', ''])
    for candidate in packet['candidates']:
        lines.extend([f"候选：{candidate['id']}，假说：{candidate['hypothesis_id']}，来源：{candidate['origin']}。", '',
                      _block(candidate['expression']), '', _block(candidate['machine_checks']), ''])
    if not packet['candidates']:
        lines.extend(['没有可解析的候选输出；保持失败、弃权或排队状态，未补造实现。', ''])
    lines.extend(['## 调用记录与失败', '', _block(packet['attempts']), '',
                  '## 实验与实际指标', ''])
    if computation is None:
        lines.extend(['尚无计算结果。没有默认零指标。', ''])
    else:
        lines.extend([_block(computation), ''])
    lines.extend(['## 范围与身份', '', _block(packet['scope']), '', _block(packet['identities']), '',
                  f"审核包对象摘要（去除 packet_digest 字段后 canonical JSON）：{packet['packet_digest']}", '',
                  '## 人工五维判断（待填写）', '', '| 判断维度 | 结论 | 原因 |', '| --- | --- | --- |'])
    lines.extend(f'| {name} |  |  |' for name in DIMENSIONS)
    lines.extend(['', '请查看原 PDF、实际表达式及结果后再通过已有评审入口提交；这里的留白不是评审记录。', ''])
    return '\n'.join(lines)


def _new_directory(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise ResearchReviewError('Review output path cannot contain symlinks or traversal', code='REVIEW_OUTPUT_INVALID')
    if path.exists():
        raise ResearchReviewError('Review export requires a new output directory', code='REVIEW_OUTPUT_EXISTS')
    return path


def _write_new(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def build_research_review(store, proposal_id, *, research_job_id=None) -> dict:
    """Read and verify a packet without files, experiments or saved judgments.

    Queued, failed and abstained requests can be inspected without invented
    output. A linked run is checked afresh, even if saved verification is true.
    """
    service = ProposalRequests(store, configs={})
    proposal = service.get(proposal_id)
    source, paper_row, paper = _source(store, proposal)
    response = _response(service, proposal)
    model_output = response['model_output'] if response else None
    draft = model_output.get('draft') if model_output else None
    output_evidence = _evidence(draft['evidence'], paper) if draft else []
    validation = next((item['validation'] for item in reversed(proposal['attempts']) if item.get('validation')), None)
    checks = validation.get('candidate_checks', []) if validation else []
    candidates = []
    for item in draft['candidates'] if draft else []:
        candidates.append({**deepcopy(item),
            'hypothesis': next((deepcopy(h) for h in draft['hypotheses'] if h['id'] == item['hypothesis_id']), None),
            'machine_checks': [deepcopy(check) for check in checks if check.get('candidate_id') == item['id']]})
    job, computation = _job(store, proposal, response, research_job_id)
    config = proposal['config']
    packet = {'schema_version': 1, 'kind': 'provider_research_review',
        'proposal': {key: deepcopy(proposal.get(key)) for key in ('id', 'status', 'created_at', 'deadline_at',
            'provider_invocations', 'accepted_attempt_id', 'accepted_response_digest', 'research_job_id', 'error')} | {
            'request_digest': digest(proposal['request']), 'purpose': proposal['request']['purpose'],
            'candidate_limit': proposal['request']['candidate_limit'], 'provider_config_id': config['config_id'],
            'provider_mode': config['mode'], 'requested_model': config['model'], 'reasoning_effort': config['reasoning_effort']},
        'identities': {'research_id': proposal['request']['research_id'], 'base_revision_id': proposal['request']['base_revision_id'],
            'paper_id': source['research']['paper_id'], 'dataset_id': source['research']['dataset_id'],
            'source_digest': proposal['context']['source_digest'], 'context_digest': proposal['context_digest'],
            'paper_sha256': source['paper_sha256'], 'paper_document_digest': source['paper_document_digest'],
            'dataset_sha256': source['dataset_sha256'], 'data_file_sha256': source['data_file_sha256'],
            'metadata_file_sha256': source['metadata_file_sha256'], 'base_task_digest': source['revision']['digest'],
            'review_exporter_source_sha256': sha256(__file__)},
        'paper': {key: deepcopy(paper[key]) for key in ('id', 'title', 'url', 'version', 'document_sha256')} | {
            'pdf_path': paper_row['pdf_path']},
        'input_evidence': _evidence(proposal['context']['evidence'], paper, required=True),
        'input_hypotheses': deepcopy(proposal['context']['hypotheses']),
        'input_request': deepcopy(proposal['request']),
        'response': response, 'output_evidence': output_evidence, 'candidates': candidates,
        'attempts': [_attempt(item) for item in proposal['attempts']], 'research_job': job, 'computation': computation,
        'semantic_fidelity': 'unverified',
        'human_review': {'status': 'pending', 'reviewer': None, 'confirmed_at': None,
            'dimensions': {name: {'outcome': None, 'reason': None} for name in DIMENSIONS}},
        'scope': {'dataset_kind': proposal['context']['capabilities']['data_kind'],
            'model_context_development': deepcopy(proposal['context']['development']),
            'context_omission_claims_apply_to_frozen_provider_context_projection_only': True,
            'model_context_contains_market_rows': False, 'model_context_contains_final_test_range': False,
            'local_complete_snapshot_and_factor_computation_may_read_test_bytes': True,
            'computed_metrics_scope': 'validation only; synthetic software validation; gross returns exclude costs',
            'human_judgments_written': 0, 'economic_semantics_verified': False, 'market_performance_claim': False}}
    # Legitimate research commit can move latest_revision_id. Check the original
    # frozen base and actual byte identities, rather than requiring it be latest.
    if digest(service.get(proposal_id)) != digest(proposal) or _source(store, proposal)[0] != source:
        raise ResearchReviewError('Review sources changed while preparing the packet')
    packet['packet_digest'] = digest(packet)
    return packet


def export_research_review(store, proposal_id, output_dir, *, research_job_id=None) -> dict:
    """Write a verified packet only to a new directory; human judgments stay blank."""
    destination = _new_directory(output_dir)
    packet = build_research_review(store, proposal_id, research_job_id=research_job_id)
    raw_json, raw_md = json_text(packet).encode(), _markdown(packet).encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _new_directory(destination)
    destination.mkdir(mode=0o700)
    _write_new(destination / 'review.json', raw_json)
    _write_new(destination / 'review.md', raw_md)
    return {'schema_version': 1, 'proposal_id': proposal_id, 'status': packet['proposal']['status'],
        'research_job_id': packet['proposal'].get('research_job_id'), 'result_present': packet['computation'] is not None,
        'human_status': 'pending', 'semantic_fidelity': 'unverified', 'packet_digest': packet['packet_digest'],
        'files': {name: {'path': str(destination / name), 'sha256': hashlib.sha256(raw).hexdigest()}
                  for name, raw in (('review.json', raw_json), ('review.md', raw_md))}}
