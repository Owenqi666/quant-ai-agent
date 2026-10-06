"""Immutable references to verified domain results; never an execution authority.

The snapshot is deliberately a projection, not a replacement for domain records.
Every read revalidates the original result and the exact reviews frozen at creation;
new reviews do not change a case and old approvals never transfer to a new result.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import re

from .db import connect, transaction
from .service import ServiceError, now
from .author_panels import _metadata_digest, _verify_metadata
from .author_studies import AuthorStudies
from .monthly_experiments import MonthlyExperiments
from .research_cases_schema import CaseCreate, CaseDetail, CasePreview
from .research_assessments import assessment_summary
from ..storage import digest, json_text, read_json

SCHEMA = """
CREATE TABLE research_cases(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL,metadata_digest TEXT NOT NULL);
CREATE INDEX research_cases_created ON research_cases(created_at,id);
CREATE TABLE research_case_receipts(idempotency_key TEXT PRIMARY KEY,request_digest TEXT NOT NULL,case_id TEXT NOT NULL REFERENCES research_cases(id),case_digest TEXT NOT NULL,case_metadata_digest TEXT NOT NULL,created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL);
"""
MAX_BYTES = 4 * 1024 * 1024
CASE_FIELDS = {'title', 'note', 'source_kind', 'source_id', 'source_digest', 'context'}
COMMON_LIMITATIONS = [
    'This is a frozen context for an existing result, not a new experiment or human approval.',
    'Review actors are local declarations, not authenticated identities; approvals apply only to their original exact result.',
    'Evidence references and literal matches do not establish semantic fidelity; human confirmation is separate.',
    'No AI provider, human-efficiency improvement, final-test conclusion or investment performance is inferred.',
]


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'research_case_[0-9a-f]{64}', value):
        raise ServiceError('Invalid research case identity', 422)


def _provenance(**values):
    return [{'label': key, 'value': value if isinstance(value, str) else json_text(value)} for key, value in values.items()]


def _result(identity, kind, result, summary):
    return {'id': identity, 'kind': kind, 'digest': digest(result), 'summary': summary, 'payload_json': json_text(result)}


def _decision(state, reason=None):
    actions = {
        'ready_for_review': ['request_human_review', 'revise_plan'],
        'data_insufficient': ['stop_data_insufficient', 'request_human_review', 'revise_plan'],
        'rules_unresolved': ['resolve_method', 'request_human_review'],
        'implementation_failed': ['request_human_review', 'revise_plan'],
    }
    return {'state': state, 'stop_reason': reason, 'allowed_actions': actions[state]}


class ResearchCases:
    def __init__(self, store):
        self.store = store

    def _daily(self, identity):
        value = self.store.get_run(identity)
        if (value['status'] != 'completed' or not value.get('verification')
                or value['verification'].get('verified') is not True or not value.get('regression_target')):
            raise ServiceError('Research context requires a completed, verified daily attempt', 409)
        state, target = value['state'], value['regression_target']
        preflight, task = state['preflight'], state['task']
        evidence = [{'id': e['id'], 'origin': 'paper', 'locator': f"{e['paper_id']} p.{e['page']}; PDF SHA256 {e['document_sha256']}",
                     'text': e['quote'], 'verification': 'literal_quote_verified'} for e in preflight['evidence'] if e.get('citation_verified') is True]
        if len(evidence) != len(task['evidence']):
            raise ServiceError('Daily evidence is not completely verified', 409)
        definitions = [{'id': h['id'], 'attribution': h['attribution'], 'text': json_text(h),
                        'evidence_ids': h['evidence_ids']} for h in task['hypotheses']]
        candidates = state['candidates']
        evaluated = sum(c['status'] == 'evaluated' for c in candidates)
        blocked = sum(c['status'] == 'blocked' for c in candidates)
        decision = _decision('ready_for_review') if evaluated else _decision(
            'data_insufficient' if blocked == len(candidates) else 'implementation_failed',
            'No candidate was evaluated; inspect the recorded per-candidate reason before changing the plan.')
        original_targets = {c['candidate_id']: c['result_digest'] for c in value['review_targets']}
        reviews = []
        for item in value['reviews']:
            if item['attempt_id'] != target['attempt_id']:
                continue
            if original_targets.get(item['candidate_id']) != item['result_digest']:
                raise ServiceError('Daily review binding differs from the verified candidate', 409)
            reviews.append({'id': item['id'], 'actor': item['source'], 'decision': item['verdict'],
                            'scope': f"candidate={item['candidate_id']}; attempt={item['attempt_id']}; result={item['result_digest']}", 'note': item['note'],
                            'assessment': item['assessment'], 'assessment_summary': item['assessment_summary']})
        manifests = [a for a in self.store.list_artifacts(identity) if a['name'] == 'manifest.json' and a['attempt_id'] == target['attempt_id']]
        if len(manifests) != 1:
            raise ServiceError('Verified daily manifest is missing', 409)
        manifest = read_json(self.store.artifact_path(identity, manifests[0]['id'])[0])
        return {**decision, 'evidence': evidence, 'definitions': definitions,
                'data_scope': f"{preflight['data_kind']}; {preflight['data_version']}; available fields: {', '.join(preflight['available_fields'])}",
                'method_scope': 'Daily factor engine on the declared validation split only; candidate expressions and modifications remain explicit.',
                'provenance': _provenance(research_id=value['research_id'], revision_id=value['revision_id'],
                    attempt_id=target['attempt_id'], full_state_digest=target['result_digest'],
                    manifest_digest=digest(manifest), signature_digest=manifest.get('signature_sha256', 'unknown'),
                    code_digest=digest(manifest.get('signature', {}).get('code', {})), evaluation=task['evaluation'], mode=value['mode']),
                'results': [_result(target['attempt_id'], 'daily_candidates', candidates,
                                   f'{evaluated}/{len(candidates)} candidates evaluated; {blocked} blocked.')],
                'reviews': reviews, 'limitations': [*COMMON_LIMITATIONS,
                    'Candidate payloads are verified public projections; full immutable state is identified separately by full_state_digest.',
                    'Existing agent mode uses deterministic repairs; it is not an LLM.']}

    def _monthly(self, identity):
        service = MonthlyExperiments(self.store)
        value = service.get(identity)
        if (value['experiment']['status'] != 'completed' or value['verification']['verified'] is not True
                or value['result'] is None or value['review_target'] is None):
            raise ServiceError('Research context requires a completed, verified monthly attempt', 409)
        result, protocol, target = value['result'], value['protocol'], value['review_target']
        manifest = read_json(service._path(identity, target['attempt_id']) / 'output' / 'manifest.json')
        status = result['status']
        if status == 'blocked':
            decision = _decision('rules_unresolved', 'The recorded paper rules remain unresolved; no portfolio result is available.')
        elif status == 'not_evaluable':
            decision = _decision('data_insufficient', 'The declared monthly experiment has no evaluable portfolio periods.')
        else:
            decision = _decision('ready_for_review')
        evidence = []
        for i, source in enumerate(protocol['sources']):
            if source.startswith('Project adaptation '):
                origin = 'project'
            elif source.startswith('Author archive V2;'):
                origin = 'author_code'
            elif source.startswith('Goyal, Jegadeesh and Subrahmanyam;'):
                origin = 'paper'
            else:
                origin = 'unknown'
            evidence.append({'id': f'monthly-source-{i+1}', 'origin': origin, 'locator': source,
                             'text': source,
                             'verification': 'project_declaration' if origin == 'project' else 'source_registry_reference'})
        definitions = [{'id': 'monthly-protocol', 'attribution': 'project_convention' if protocol['config']['mode'] == 'project' else 'unresolved',
                        'text': json_text(protocol['config']), 'evidence_ids': [item['id'] for item in evidence]}]
        reviews = []
        for item in value['reviews']:
            if item['attempt_id'] != target['attempt_id']:
                continue
            if item['result_digest'] != target['result_digest']:
                raise ServiceError('Monthly review binding differs from the verified result', 409)
            reviews.append({'id': item['id'], 'actor': item['source'], 'decision': item['verdict'],
                            'scope': f"attempt={item['attempt_id']}; result={item['result_digest']}", 'note': item['note'],
                            'assessment': None, 'assessment_summary': assessment_summary(None, item['source'])})
        return {**decision, 'evidence': evidence, 'definitions': definitions,
                'data_scope': f"{result['data_kind']}; source={result['source_id']}; no author MAT inputs or actual-market performance claim.",
                'method_scope': 'Declared monthly MOM/ID project method on controlled daily-return fixtures; target-weight transaction cost and NAV proxies.',
                'provenance': _provenance(attempt_id=target['attempt_id'], result_digest=target['result_digest'],
                    input_digest=value['verification']['input_digest'], protocol_id=protocol['id'], protocol_digest=protocol['digest'],
                    manifest_digest=digest(manifest), code_digest=digest({key: value for key, value in manifest['files'].items() if key.startswith('source/')}),
                    environment_digest=manifest['files']['environment.json'],
                    semantics_version=result['semantics_version'], config=result['config'], reference_passed=value['verification']['reference_passed']),
                'results': [_result(target['attempt_id'], 'monthly_portfolio', result, f"Monthly result: {status}; {len(result['months'])} declared months.")],
                'reviews': reviews, 'limitations': [*COMMON_LIMITATIONS, *protocol['unresolved'], *result['warnings'],
                    'This result cannot be attributed to the author-source eligibility studies or treated as full paper replication.']}

    def _author(self, identity):
        service = AuthorStudies(self.store)
        value = service.get(identity)
        result = value['result']
        blocked = result['summary']['status'] == 'screen_blocked'
        decision = _decision('data_insufficient' if blocked else 'rules_unresolved',
            'The predeclared asset threshold is not met in every development month; stop this candidate or submit an explicit reviewed revision.' if blocked else
            'Eligibility counts pass, but author-source portfolio method, universe and holding labels have not been executed.')
        evidence = [{'id': item['id'], 'origin': item['origin'], 'locator': item['locator'], 'text': item['claim'],
                     'verification': 'project_declaration' if item['origin'] == 'project' else 'source_registry_reference'}
                    for item in result['evidence']['citations']]
        definitions = [{'id': 'eligibility-plan', 'attribution': 'project_convention', 'text': json_text(result['plan']),
                        'evidence_ids': [e['id'] for e in evidence]}]
        reviews = [{'id': r['id'], 'actor': r['actor'], 'decision': r['decision'],
                    'scope': f"study={r['study_id']}; digest={r['study_digest']}", 'note': r['note'],
                    'assessment': None, 'assessment_summary': assessment_summary(None, r['actor'])}
                   for r in service.reviews(identity)['items']]
        return {**decision, 'evidence': evidence, 'definitions': definitions,
                'data_scope': 'Imported aggregate author-data scan; public archive is perturbed. Raw-file hashes are declared metadata; this HTTP context checks aggregate consistency only.',
                'method_scope': 'Label-free eligibility counts on all source rows. No author-source portfolio execution, sorting, weights or investment returns.',
                'provenance': _provenance(study_digest=value['digest'], source_title=value['title'], source_note=value['note'], input_digest=result['input_digest'], plan_digest=result['plan_digest'],
                    source_filename=result['source']['filename'], source_sha256_claim=result['source']['sha256'],
                    verification_scope=value['verification_scope'], raw_source_reverified=False, execution_ready=False,
                    semantics_version=result['semantics_version'], paper_pdf_sha256=result['evidence']['pdf_sha256'],
                    parent_study_id=value['parent_study_id'] or 'none', parent_review_id=value['parent_review_id'] or 'none'),
                'results': [_result(identity, 'author_eligibility', result,
                    f"{result['summary']['status']}; qualifying months {result['summary']['months_meeting_threshold']}/{result['summary']['months']}; execution_ready=false.")],
                'reviews': reviews, 'limitations': [*COMMON_LIMITATIONS, *result['limitations'],
                    'Imported count provenance may be synthetic; consult the original study note and plan rationale. Research hypothesis semantics are not independently confirmed.']}

    def _industry(self, identity):
        """Project one verified industry attempt; author/fixture authority is separate."""
        from .industry_mom import IndustryMomExperiments
        from .industry_mom_schema import IndustryMomExperimentDetail
        from .. import mom_only, mom_only_workflow

        try:
            value = IndustryMomExperimentDetail.model_validate(IndustryMomExperiments(self.store).get(identity)).model_dump()
            experiment, source, verification, target = (value[key] for key in
                                                        ('experiment', 'source', 'verification', 'review_target'))
            if (experiment['id'] != identity or experiment['status'] != 'completed'
                    or verification is None or target is None or value['result_json'] is None
                    or value['reference_json'] is None or value['report_markdown'] is None):
                raise ValueError('A completed verified industry attempt is required')
            result, reference = json.loads(value['result_json']), json.loads(value['reference_json'])
            method, config = json.loads(source['method_json']), json.loads(source['config_json'])
            mom_only.validate_config(config)
            mom_only_workflow._validate_method(method, config, source['archive_sha256'])
            attempts = [item for item in value['attempts'] if item['id'] == target['attempt_id']]
            if (len(attempts) != 1 or attempts[0]['status'] != 'completed'
                    or target['attempt_id'] != experiment['attempt_id']
                    or target['attempt_id'] != verification['attempt_id']
                    or target['result_digest'] != digest(result)
                    or target['result_digest'] != verification['result_digest']
                    or attempts[0]['result_digest'] != target['result_digest']
                    or experiment['source_id'] != source['id'] or verification['source_id'] != source['id']
                    or experiment['source_digest'] != source['digest'] or verification['source_digest'] != source['digest']
                    or experiment['input_digest'] != verification['input_digest']
                    or verification['source_input_digest'] != source['input_digest']
                    or verification['source_manifest_digest'] != source['manifest_digest']
                    or source['config_digest'] != digest(config) or experiment['config_digest'] != digest(config)
                    or verification['config_digest'] != digest(config)
                    or source['method_digest'] != method['contract_digest']
                    or verification['method_digest'] != method['contract_digest']
                    or verification['archive_sha256'] != source['archive_sha256']
                    or result['data_kind'] != 'market_derived_portfolio_returns'
                    or result['asset_kind'] != 'industry_portfolio'
                    or result['research_scope'] != 'project_modification'
                    or result['source_id'] != mom_only.SOURCE_ID or result['study_id'] != mom_only.STUDY_ID
                    or result['semantics_version'] != mom_only.SEMANTICS_VERSION
                    or result['return_semantics'] != 'monthly_total_return_decimal'
                    or result['human_judgment'] is not None or result['reserved_evaluated'] is not False
                    or result['config'] != config or result['config_digest'] != digest(config)
                    or result['input_digest'] != verification['panel_digest']
                    or result['source']['archive_sha256'] != source['archive_sha256']
                    or result['source']['source_contract'] != mom_only.SOURCE_CONTRACT
                    or result['source']['section'] != mom_only.SOURCE_SECTION
                    or reference.get('supported') is not True or reference.get('passed') is not True
                    or reference.get('issues')):
                raise ValueError('Industry source/attempt/result/method references disagree')
            if result['status'] not in {'evaluated', 'partial', 'not_evaluable'}:
                raise ValueError('Unsupported industry result state')

            evidence = [{'id': 'industry-paper-' + item['id'], 'origin': 'paper',
                         'locator': f"{method['paper']['id']} p.{item['page_1_based']}; PDF SHA256 {item['document_sha256']}; normalized span {item['normalized_start']}..{item['normalized_end_exclusive']}",
                         'text': item['quote'], 'verification': 'literal_quote_verified'}
                        for item in method['evidence']]
            for item in method['sources']:
                related = {key: method['paper_signal'][key] for key in
                           ('compounding_evidence', 'target_alignment_evidence')
                           if method['paper_signal'][key]['source_id'] == item['id']}
                evidence.append({'id': 'industry-author-' + item['id'], 'origin': 'author_code',
                    'locator': f"Author archive {item['archive_doi']} V{item['archive_version']}; {item['id']}.m SHA256 {item['sha256']}",
                    'text': json_text({'source': item, 'signal_references': related, 'executed': False}),
                    'verification': 'source_registry_reference'})
            project = {'config': config, 'input_and_missing_policy': method['input_and_missing_policy'],
                       'illustrative_defaults': method['illustrative_defaults']}
            evidence.append({'id': 'industry-project-method', 'origin': 'project',
                             'locator': 'Fixed industry project contract; method digest ' + source['method_digest'],
                             'text': json_text(project), 'verification': 'project_declaration'})
            window_ids = ['industry-paper-' + item for item in method['paper_signal']['window_evidence_ids']]
            definitions = [
                {'id': 'industry-mom-paper-signal', 'attribution': 'paper_original',
                 'text': json_text(method['paper_signal']),
                 'evidence_ids': window_ids + ['industry-author-SetupDataA', 'industry-author-Table8A']},
                {'id': 'industry-mom-project-method', 'attribution': 'project_convention',
                 'text': json_text(project), 'evidence_ids': ['industry-project-method']},
            ]
            decision = _decision('data_insufficient', 'The fixed industry result has no evaluable portfolio periods.') if result['status'] == 'not_evaluable' else _decision('ready_for_review')
            return {**decision, 'evidence': evidence, 'definitions': definitions,
                'data_scope': 'market_derived_portfolio_returns; industry_portfolio; source=' + result['source_id'] + '; 49 published value-weighted industry return series, not individual stocks or controlled fixtures.',
                'method_scope': 'Project modification: complete eleven H-12..H-2 natural monthly returns; skip H-1; no fill. Development 2010-01..2011-12, formation input 2009-01..2011-12; reserved 2012-01..2013-12 is not evaluated. Gross-only MOM and same-formation-sample long-only baseline with different net exposures.',
                'provenance': _provenance(experiment_id=identity, attempt_id=target['attempt_id'],
                    result_digest=target['result_digest'], source_id=source['id'], source_digest=source['digest'],
                    market_source_id=result['source_id'], archive_sha256=source['archive_sha256'],
                    source_input_digest=source['input_digest'], source_result_digest=source['result_digest'],
                    source_manifest_digest=source['manifest_digest'], source_code_digest=source['code_digest'],
                    input_digest=verification['input_digest'], panel_digest=verification['panel_digest'],
                    manifest_digest=verification['manifest_digest'], code_digest=verification['code_digest'],
                    environment_digest=verification['environment_digest'], config_digest=verification['config_digest'],
                    method_digest=source['method_digest'], paper_pdf_sha256=method['paper']['document_sha256'],
                    reference_digest=digest(reference), reference_passed=verification['reference_passed'],
                    report_sha256=hashlib.sha256(value['report_markdown'].encode('utf-8')).hexdigest(),
                    semantics_version=result['semantics_version'], development=config['development'], reserved=config['reserved'],
                    reserved_evaluated=False, human_review='pending', legacy_author_execution_ready=False),
                'results': [_result(target['attempt_id'], 'industry_mom_portfolio', result,
                    f"Industry MOM gross result: {result['status']}; {len(result['months'])} declared development months; reserved_evaluated=false.")],
                'reviews': [], 'limitations': [*COMMON_LIMITATIONS, *method['limitations'], *result['warnings'],
                    'Technical ready_for_review does not establish human semantic approval. Exact claims use the existing independent claim-review ledger.',
                    'The original author-source eligibility task and all its blockers remain independent and unchanged.']}
        except ServiceError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ServiceError('Research context requires an intact completed industry MOM attempt with exact source and result bindings', 409) from exc

    def _preview(self, source_kind, source_id, review_ids=None, *, legacy_reviews=False):
        if (source_kind not in ('daily_run', 'monthly_experiment', 'author_study', 'industry_mom_experiment')
                or not isinstance(source_id, str) or not source_id or len(source_id) > 200):
            raise ServiceError('Invalid research context source kind or identity', 422)
        context = {'daily_run': self._daily, 'monthly_experiment': self._monthly, 'author_study': self._author,
                   'industry_mom_experiment': self._industry}[source_kind](source_id)
        if review_ids is not None:
            by_id = {review['id']: review for review in context['reviews']}
            if not set(review_ids) <= set(by_id):
                raise ServiceError('A frozen review is missing from its exact source result', 409)
            context['reviews'] = [by_id[identity] for identity in review_ids]
        if legacy_reviews:
            for review in context['reviews']:
                review.pop('assessment', None)
                review.pop('assessment_summary', None)
        body = {'source_kind': source_kind, 'source_id': source_id, 'context': context}
        preview = {**body, 'source_digest': digest(body)}
        try:
            normalized = CasePreview.model_validate(preview).model_dump(exclude_unset=True)
            if json_text(normalized) != json_text(preview):
                raise ValueError('Projection normalization differs')
            if len(json_text(preview).encode()) > MAX_BYTES:
                raise ServiceError('Research context exceeds 4 MiB', 413)
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError('Source result exceeds or fails the research context contract', 409) from exc
        return preview

    def preview(self, source_kind, source_id):
        return self._preview(source_kind, source_id)

    def _record(self, row):
        try:
            # The generic author-study parser has a smaller bound; case result
            # projections legitimately contain up to 4 MiB, so validate here.
            if len(row['payload'].encode()) > MAX_BYTES:
                raise ValueError('Case payload exceeds its limit')
            body = json.loads(row['payload'])
            if set(body) != CASE_FIELDS or json_text(body) != row['payload']:
                raise ValueError('Noncanonical case record')
            fingerprint = digest(body)
            if row['digest'] != fingerprint or row['id'] != 'research_case_' + fingerprint:
                raise ValueError('Case content identity differs')
            _verify_metadata(row)
            detail = {**body, 'id': row['id'], 'digest': row['digest'], 'created_at': row['created_at']}
            if json_text(CaseDetail.model_validate(detail).model_dump(exclude_unset=True)) != json_text(detail):
                raise ValueError('Case response is not canonical')
            frozen_reviews = body['context']['reviews']
            # v17 used a smaller immutable projection. Verify that same
            # projection, rather than re-signing historical records or tools.
            shapes = {('assessment' in r, 'assessment_summary' in r) for r in frozen_reviews}
            if len(shapes) > 1 or any(a != b for a, b in shapes):
                raise ValueError('Mixed review projection versions')
            current = self._preview(body['source_kind'], body['source_id'], [r['id'] for r in frozen_reviews],
                                    legacy_reviews=shapes == {(False, False)})
            if any(current[key] != body[key] for key in ('source_kind', 'source_id', 'source_digest', 'context')):
                raise ValueError('Source result or frozen reviews changed')
            return detail
        except ServiceError:
            raise
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            raise ServiceError('Research case failed integrity verification', 409) from exc

    def create(self, title, note, source_kind, source_id, source_digest, idempotency_key):
        try:
            request = CaseCreate.model_validate(dict(title=title, note=note, source_kind=source_kind, source_id=source_id,
                                                     source_digest=source_digest, idempotency_key=idempotency_key)).model_dump()
            json_text(request).encode('utf-8')
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ServiceError('Invalid research case request', 422) from exc
        fingerprint = digest({key: value for key, value in request.items() if key != 'idempotency_key'})
        # One writer fence prevents concurrent result/review publication while
        # domain services revalidate source files. No domain rows are changed.
        with transaction(self.store.db_path) as connection:
            receipt = connection.execute('SELECT * FROM research_case_receipts WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if receipt is not None:
                fields = {key: receipt[key] for key in receipt.keys() if key != 'receipt_digest'}
                if digest(fields) != receipt['receipt_digest']:
                    raise ServiceError('Research case receipt failed integrity verification', 409)
                if receipt['request_digest'] != fingerprint:
                    raise ServiceError('Idempotency key was already used for another research case request', 409)
                row = connection.execute('SELECT * FROM research_cases WHERE id=?', (receipt['case_id'],)).fetchone()
                if row is None or row['digest'] != receipt['case_digest'] or row['metadata_digest'] != receipt['case_metadata_digest']:
                    raise ServiceError('Research case receipt binding is invalid', 409)
                return self._record(row)
            preview = self.preview(source_kind, source_id)
            if preview['source_digest'] != source_digest:
                raise ServiceError('Source context changed; preview the exact result before creating a case', 409)
            body = {'title': title, 'note': note, **preview}
            identity_digest = digest(body); identity = 'research_case_' + identity_digest
            row = connection.execute('SELECT * FROM research_cases WHERE id=?', (identity,)).fetchone()
            if row is None:
                timestamp = now()
                row = dict(id=identity, payload=json_text(body), digest=identity_digest, created_at=timestamp,
                           metadata_digest=_metadata_digest(identity, identity_digest, timestamp))
                if len(row['payload'].encode()) > MAX_BYTES:
                    raise ServiceError('Research context exceeds 4 MiB', 413)
                connection.execute('INSERT INTO research_cases VALUES (?,?,?,?,?)', tuple(row[key] for key in
                                   ('id', 'payload', 'digest', 'created_at', 'metadata_digest')))
            detail = self._record(row)
            receipt_body = {'idempotency_key': idempotency_key, 'request_digest': fingerprint, 'case_id': identity,
                            'case_digest': identity_digest, 'case_metadata_digest': row['metadata_digest'], 'created_at': now()}
            connection.execute('INSERT INTO research_case_receipts VALUES (?,?,?,?,?,?,?)',
                               (*receipt_body.values(), digest(receipt_body)))
            if source_kind == 'industry_mom_experiment' and self._record(row) != detail:
                raise ServiceError('Industry source changed before research case publication', 409)
            return deepcopy(detail)

    def get(self, identity):
        _identity(identity)
        with closing(connect(self.store.db_path)) as connection:
            row = connection.execute('SELECT * FROM research_cases WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ServiceError('Research case not found', 404)
            return self._record(row)

    def list(self, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ServiceError('Case pagination requires limit 1..100 and offset 0..100000', 422)
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            total = connection.execute('SELECT COUNT(*) FROM research_cases').fetchone()[0]
            rows = connection.execute('SELECT * FROM research_cases ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
            items = []
            for row in rows:
                detail = self._record(row)
                items.append({key: detail[key] for key in ('id', 'digest', 'created_at', 'title', 'note', 'source_kind', 'source_id', 'source_digest')} |
                             {key: detail['context'][key] for key in ('state', 'stop_reason', 'allowed_actions')})
            return {'items': items, 'total': total, 'limit': limit, 'offset': offset}

    def markdown(self, identity):
        detail = self.get(identity); context = detail['context']
        lines = ['# ' + detail['title'], '', detail['note'], '', f"Case: {detail['id']}", f"Digest: {detail['digest']}",
                 f"Source: {detail['source_kind']} / {detail['source_id']}", f"Source digest: {detail['source_digest']}", '',
                 '## Decision', '', 'State: ' + context['state'], 'Stop reason: ' + (context['stop_reason'] or 'none'),
                 'Allowed actions: ' + ', '.join(context['allowed_actions']), '', '## Data and method', '', context['data_scope'], '', context['method_scope'],
                 '', '## Evidence and definitions', '']
        lines += [f"- {e['id']} ({e['origin']}; {e['verification']}): {e['text']} — {e['locator']}" for e in context['evidence']]
        for definition in context['definitions']:
            lines += ['', f"### {definition['id']} ({definition['attribution']})", '', definition['text']]
        lines += ['', '## Computed results', '']
        for result in context['results']:
            lines += [result['summary'], f"Result: {result['id']} / {result['digest']}", '', '```json', result['payload_json'].rstrip(), '```', '']
        lines += ['## Original review snapshots', '']
        lines += [f"- {r['id']} / {r['actor']} / {r['decision']}: {r['note']} ({r['scope']})" for r in context['reviews']] or ['No reviews frozen.']
        for review in context['reviews']:
            lines += ['', f"Assessment for {review['id']}: " +
                      (review['assessment_summary']['semantic_status'] if review.get('assessment_summary') else 'legacy unknown')]
            if review.get('assessment') is not None:
                lines += ['```json', json_text(review['assessment']).rstrip(), '```']
        lines += ['', '## Provenance', ''] + [f"- {item['label']}: {item['value']}" for item in context['provenance']]
        lines += ['', '## Limitations', ''] + ['- ' + item for item in context['limitations']] + ['']
        return '\n'.join(lines)
