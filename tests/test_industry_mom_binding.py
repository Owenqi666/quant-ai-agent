"""Real synthetic industry lifecycle → exact Case/claim/review contracts.

Paper/author bytes are isolated test stubs, never human labels or market proof.
The source parser, MOM calculator, numerical reference and durable services run.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from paper_alpha import eligibility, mom_only_workflow
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.claim_reviews import ClaimReviews
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import digest, json_text
import tests.test_mom_only_workflow as synthetic_workflow


class IndustryMomBindingTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing complete synthetic workflow fixture. The only
        # patches concern the fixture PDF/author evidence and its fixed hashes.
        self.fixture = synthetic_workflow.WorkflowIntegrityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture._run('original-artifact')
        self.store = Store(self.root / 'workspace')
        from paper_alpha.server.industry_mom import IndustryMomExperiments, IndustryMomSources
        self.sources = IndustryMomSources(self.store)
        self.experiments = IndustryMomExperiments(self.store)
        self.source = self.sources.register(self.root / 'original-artifact',
            'Synthetic industry fixture', 'Not downloaded market data or human evidence.', 'source')
        self.cases = ResearchCases(self.store)
        self.claims = ResearchClaims(self.store)
        self.reviews = ClaimReviews(self.store)

    def completed(self):
        created = self.experiments.create(self.source['id'], self.source['digest'], 'experiment')
        identity = created['experiment_id']
        job = self.experiments.claim('binding-worker')
        self.assertEqual(job['id'], identity)
        # Use the service-owned registered copy, as the real runner does. Only
        # its PDF/author evidence is replaced by isolated fixture stubs.
        mom_only_workflow.run(job['source_path'], job['config_path'],
            job['method_path'], Path(job['output_dir']), job['source_sha256'],
            job['source_receipt_path'], evidence_dir=job['evidence_dir'])
        self.experiments.finish(identity, 'binding-worker', job['attempt_id'], 'completed')
        value = self.experiments.get(identity)
        self.assertEqual(value['experiment']['status'], 'completed')
        self.assertTrue(value['verification']['calculation_verified'])
        return identity, job, value

    def bind(self, identity, key='case', **changes):
        preview = self.cases.preview('industry_mom_experiment', identity)
        request = {'title': 'Synthetic industry research', 'note': 'Test fixture only.',
            'source_kind': 'industry_mom_experiment', 'source_id': identity,
            'source_digest': preview['source_digest'], 'idempotency_key': key}
        return self.cases.create(**(request | changes))

    def metric(self, case, pointer='/summary/0/mean_gross_return'):
        result = case['context']['results'][0]
        return {'id': 'gross-metric', 'kind': 'metric', 'attribution': 'project_convention',
            'text': 'Untrusted narrative incorrectly claims 999999.', 'evidence_ids': [],
            'metric_references': [{'case_id': case['id'], 'case_digest': case['digest'],
                'result_id': result['id'], 'result_digest': result['digest'], 'pointer': pointer}]}

    def review_request(self, batch, key='review', outcome='passed'):
        target = self.reviews.target(batch['id'], 'gross-metric')
        return {'claims_id': batch['id'], 'claim_id': 'gross-metric',
            'expected_target_digest': target['target_digest'], 'source': 'automation',
            'reviewer': 'Synthetic control fixture, not a person',
            'confirmed_at': '2020-01-01T00:00:00+00:00',
            'dimensions': {name: {'outcome': outcome, 'reason': 'Isolated automation test, not human quality evidence.'}
                           for name in DIMENSIONS},
            'supersedes_id': None, 'expected_supersedes_digest': None, 'idempotency_key': key}

    def assert_error(self, callback, status=409):
        with self.assertRaises(ServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.status, status)

    def test_exact_case_evidence_attribution_and_source_result_bindings(self):
        study_scan = eligibility.demo_scan()
        study_scan['plan']['minimum_assets'] = 100
        study_scan['plan_digest'] = digest(study_scan['plan'])
        study = AuthorStudies(self.store).create('Old blocked author fixture', '', study_scan, None, [], 'author')
        old_preview = self.cases.preview('author_study', study['id'])
        old_case = self.cases.create('Old author case', '', 'author_study', study['id'], old_preview['source_digest'], 'old-case')
        before = self.store._read('SELECT * FROM author_studies')
        identity, job, value = self.completed()
        case = self.bind(identity)
        self.assertEqual(case['context']['state'], 'ready_for_review')
        self.assertEqual(case['source_kind'], 'industry_mom_experiment')
        result = case['context']['results'][0]
        self.assertEqual(result['kind'], 'industry_mom_portfolio')
        self.assertEqual(result['id'], job['attempt_id'])
        self.assertEqual(result['digest'], value['review_target']['result_digest'])
        self.assertEqual(json.loads(result['payload_json']), json.loads(value['result_json']))
        self.assertIn('market_derived_portfolio_returns', case['context']['data_scope'])
        self.assertIn('industry_portfolio', case['context']['data_scope'])
        self.assertEqual(case['context']['reviews'], [])
        origins = [item['origin'] for item in case['context']['evidence']]
        self.assertEqual(origins.count('paper'), 4)
        self.assertEqual(origins.count('author_code'), 6)
        self.assertEqual(origins.count('project'), 1)
        paper = next(item for item in case['context']['evidence'] if item['id'] == 'industry-paper-mom-target-month-window')
        self.assertEqual(paper['verification'], 'literal_quote_verified')
        self.assertIn('p.4', paper['locator'])
        self.assertIn('normalized span', paper['locator'])
        definitions = {item['id']: item for item in case['context']['definitions']}
        self.assertEqual(definitions['industry-mom-paper-signal']['attribution'], 'paper_original')
        self.assertEqual(definitions['industry-mom-project-method']['attribution'], 'project_convention')
        provenance = {item['label']: item['value'] for item in case['context']['provenance']}
        for name in ('input_digest', 'panel_digest', 'manifest_digest', 'code_digest',
                     'environment_digest', 'config_digest', 'method_digest', 'archive_sha256'):
            self.assertEqual(provenance[name], value['verification'][name])
        self.assertEqual(provenance['source_digest'], self.source['digest'])
        self.assertEqual(provenance['source_manifest_digest'], self.source['manifest_digest'])
        self.assertEqual(self.cases.get(case['id']), case)
        markdown = self.cases.markdown(case['id'])
        self.assertIn('Source: industry_mom_experiment / ' + identity, markdown)
        self.assertIn('Industry MOM gross result: evaluated', markdown)
        self.assertIn('Result: ' + job['attempt_id'] + ' / ' + result['digest'], markdown)
        self.assertEqual(self.store._read('SELECT * FROM author_studies'), before)
        self.assertEqual(self.cases.get(old_case['id']), old_case)
        self.assertEqual(old_case['context']['state'], 'data_insufficient')
        self.assertEqual(self.reviews.list()['total'], 0)

    def test_queued_running_failed_and_foreign_source_cannot_form_case(self):
        created = self.experiments.create(self.source['id'], self.source['digest'], 'experiment')
        identity = created['experiment_id']
        self.assert_error(lambda: self.cases.preview('industry_mom_experiment', identity))
        job = self.experiments.claim('binding-worker')
        self.assert_error(lambda: self.cases.preview('industry_mom_experiment', identity))
        self.experiments.finish(identity, 'binding-worker', job['attempt_id'], 'failed', error='Synthetic failure')
        self.assert_error(lambda: self.cases.preview('industry_mom_experiment', identity))
        with self.assertRaises(ServiceError) as caught:
            self.cases.preview('industry_mom_experiment', self.source['id'])
        self.assertIn(caught.exception.status, (404, 422))
        self.assertEqual(self.cases.list()['total'], 0)

    def test_wrong_domain_current_attempt_and_method_projection_are_rejected(self):
        identity, _, detail = self.completed()
        from paper_alpha.server.industry_mom import IndustryMomExperiments
        for change in ('domain', 'target', 'method', 'config', 'reference', 'input'):
            value = deepcopy(detail)
            if change == 'domain':
                result = json.loads(value['result_json'])
                result['data_kind'] = 'controlled_fixture'
                value['result_json'] = json_text(result)
                value['review_target']['result_digest'] = digest(result)
                value['verification']['result_digest'] = digest(result)
                next(item for item in value['attempts'] if item['id'] == value['review_target']['attempt_id'])['result_digest'] = digest(result)
            elif change == 'target':
                value['verification']['attempt_id'] = 'foreign-attempt'
            elif change == 'method':
                method = json.loads(value['source']['method_json'])
                method['paper_signal']['formula'] = 'Return(i,H+1)'
                method['contract_digest'] = digest({key:item for key,item in method.items() if key != 'contract_digest'})
                value['source']['method_json'] = json_text(method)
                value['source']['method_digest'] = value['verification']['method_digest'] = method['contract_digest']
            elif change == 'config':
                value['source']['config_digest'] = '0' * 64
            elif change == 'reference':
                reference = json.loads(value['reference_json']); reference['passed'] = False
                value['reference_json'] = json_text(reference)
            else:
                value['experiment']['input_digest'] = '0' * 64
            with self.subTest(change=change), patch.object(IndustryMomExperiments, 'get', return_value=value):
                self.assert_error(lambda: self.cases.preview('industry_mom_experiment', identity))

    def test_stale_preview_concurrent_replay_and_changed_request(self):
        identity, _, _ = self.completed()
        self.assert_error(lambda: self.bind(identity, source_digest='0' * 64))
        self.assertEqual(self.cases.list()['total'], 0)
        with ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(lambda _: self.bind(identity), range(3)))
        self.assertTrue(all(item == values[0] for item in values))
        self.assertEqual(self.cases.list()['total'], 1)
        self.assertEqual(len(self.store._read('SELECT * FROM research_case_receipts')), 1)
        self.assert_error(lambda: self.bind(identity, title='Another title'))

    def test_claim_numeric_values_are_resolved_from_industry_gross_outputs(self):
        identity, _, detail = self.completed()
        case = self.bind(identity)
        result = json.loads(detail['result_json'])
        index = next(i for i,item in enumerate(result['summary']) if item['strategy_id'] == 'momentum')
        pointers = [(f'/summary/{index}/mean_gross_return', result['summary'][index]['mean_gross_return']),
            (f'/summary/{index}/terminal_gross_return_index', result['summary'][index]['terminal_gross_return_index']),
            ('/months/0/strategies/0/gross_return', result['months'][0]['strategies'][0]['gross_return']),
            ('/paired_comparison/mean_gross_difference', result['paired_comparison']['mean_gross_difference'])]
        for number,(pointer,expected) in enumerate(pointers):
            draft = self.metric(case, pointer)
            batch = self.claims.create(case['id'], case['digest'], [draft], f'claims-{number}')
            claim = batch['claims'][0]
            self.assertEqual(claim['metrics'][0]['value'], expected)
            self.assertEqual(claim['authoritative_display'],
                case['context']['results'][0]['id'] + ':' + pointer + ' = ' + json_text(expected).strip())
            self.assertNotEqual(claim['metrics'][0]['value'], 999999)
            self.assertEqual(claim['actor'], 'automation')
            self.assertEqual(claim['semantic_fidelity'], 'unverified')
            target = self.reviews.target(batch['id'], draft['id'])
            self.assertEqual(target['source_kind'], 'industry_mom_experiment')
            self.assertEqual(target['source_id'], identity)
            self.assertEqual(target['results'][0]['kind'], 'industry_mom_portfolio')
        self.assertEqual(self.reviews.list()['total'], 0)

    def test_net_proxy_nonmetric_unsafe_and_forged_value_claims_are_rejected(self):
        identity, _, _ = self.completed()
        case = self.bind(identity)
        for pointer in ('/summary/0/mean_net_return_proxy', '/months/0/strategies/0/nav_proxy',
                        '/summary/0/strategy_id', '/config/min_formation_assets', '/reserved_evaluated',
                        '/months/00/strategies/0/gross_return', '/summary/-1/mean_gross_return',
                        '/__proto__/value', '/months/0/strategies/0/cost_proxy'):
            with self.subTest(pointer=pointer):
                self.assert_error(lambda: self.claims.create(case['id'], case['digest'], [self.metric(case, pointer)], 'bad'), 422)
        draft = self.metric(case); draft['metric_references'][0]['value'] = 999
        self.assert_error(lambda: self.claims.create(case['id'], case['digest'], [draft], 'bad'), 422)
        self.assertEqual(self.claims.list()['total'], 0)

    def test_evidence_origin_and_exact_claim_automation_review_remain_separate(self):
        identity, _, _ = self.completed()
        case = self.bind(identity)
        project = {'id': 'project', 'kind': 'project_rule', 'attribution': 'project_convention',
            'text': 'Industry weights are project choices.', 'evidence_ids': ['industry-project-method'], 'metric_references': []}
        bad = dict(project, kind='evidence_statement', attribution='paper_original')
        self.assert_error(lambda: self.claims.create(case['id'], case['digest'], [bad], 'bad'), 422)
        batch = self.claims.create(case['id'], case['digest'], [self.metric(case), project], 'claims')
        review = self.reviews.create(**self.review_request(batch))
        status = self.reviews.status(batch['id'], 'gross-metric')
        self.assertEqual(status['human_records'], 0)
        self.assertEqual(status['automation_records'], 1)
        self.assertEqual(status['human_declared_status'], 'pending')
        self.assertFalse(review['original_result_approval'])
        self.assertIsNone(status['semantic_quality_score'])
        draft = self.metric(case); draft['text'] += ' Reworded.'
        changed = self.claims.create(case['id'], case['digest'], [draft], 'changed')
        request = self.review_request(changed, key='stale-review')
        request['expected_target_digest'] = review['target']['target_digest']
        self.assert_error(lambda: self.reviews.create(**request))
        self.assertEqual(self.reviews.status(changed['id'], 'gross-metric')['records'], 0)
        self.assertEqual(self.cases.get(case['id']), case)

    def test_actual_result_byte_tamper_blocks_case_claim_and_review_reads(self):
        identity, job, _ = self.completed()
        case = self.bind(identity)
        batch = self.claims.create(case['id'], case['digest'], [self.metric(case)], 'claims')
        (Path(job['output_dir']) / 'result.json').write_text('{}', encoding='utf-8')
        self.assert_error(lambda: self.cases.get(case['id']))
        self.assert_error(lambda: self.claims.get(batch['id']))
        self.assert_error(lambda: self.reviews.target(batch['id'], 'gross-metric'))

    def test_final_source_fence_rolls_back_case_and_creation_receipt(self):
        identity, job, _ = self.completed()
        original = self.cases._record
        changed = False
        def record(row):
            nonlocal changed
            value = original(row)
            if not changed:
                changed = True
                (Path(job['output_dir']) / 'result.json').write_text('{}', encoding='utf-8')
            return value
        with patch.object(self.cases, '_record', side_effect=record):
            self.assert_error(lambda: self.bind(identity))
        self.assertEqual(self.store._read('SELECT * FROM research_cases'), [])
        self.assertEqual(self.store._read('SELECT * FROM research_case_receipts'), [])

    def test_backup_relocation_preserves_exact_case_claim_and_automation_history(self):
        identity, _, _ = self.completed()
        case = self.bind(identity)
        batch = self.claims.create(case['id'], case['digest'], [self.metric(case)], 'claims')
        review = self.reviews.create(**self.review_request(batch))
        create_backup(self.store.root, self.root / 'backup')
        restore_backup(self.root / 'backup', self.root / 'restored')
        restored = Store(self.root / 'restored')
        self.assertEqual(ResearchCases(restored).get(case['id']), case)
        self.assertEqual(ResearchClaims(restored).get(batch['id']), batch)
        self.assertEqual(ClaimReviews(restored).get(review['id']), review)
        self.assertEqual(ClaimReviews(restored).status(batch['id'], 'gross-metric')['human_records'], 0)


if __name__ == '__main__':
    unittest.main()
