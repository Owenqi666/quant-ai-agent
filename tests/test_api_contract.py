"""HTTP contract acceptance, including real computation and negative projections."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from paper_alpha.server.api import create_app
from paper_alpha.storage import json_text
from paper_alpha.workflow import run_task
from scripts.export_api_contract import generate, main, validate_vocabulary

ROOT = Path(__file__).resolve().parents[1]


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root / 'workspace')
        self.client = TestClient(self.app)
        self.store = self.app.state.store

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_response_models_cover_all_json_business_routes(self):
        schema = self.app.openapi()
        file_routes = {'/api/papers/{paper_id}/pdf', '/api/runs/{run_id}/report',
                       '/api/runs/{run_id}/artifacts/{artifact_id}', '/api/feedback-summary/export',
                       '/api/monthly-experiments/{experiment_id}/reports/{report_id}/markdown',
                       '/api/author-panels/{panel_id}/markdown', '/api/author-studies/{study_id}/markdown',
                       '/api/research-cases/{case_id}/markdown',
                       '/api/research-jobs/{job_id}/markdown', '/api/research-claims/{claims_id}/markdown',
                       '/api/domain-research-jobs/{job_id}/markdown', '/api/semantic-material-sources/{source_id}',
                       '/api/research-bindings/{binding_id}/markdown',
                       '/api/industry-mom-experiments/{experiment_id}/report',
                       '/api/review-materials/{claims_id}/sources/{source_id}'}
        for path, methods in schema['paths'].items():
            for operation in methods.values():
                for status, response in operation['responses'].items():
                    if status.startswith('2') and path not in file_routes:
                        with self.subTest(path=path, status=status):
                            self.assertTrue(response['content']['application/json']['schema'])
                    if status == '422':
                        self.assertEqual(response['content']['application/json']['schema'],
                                         {'$ref': '#/components/schemas/ErrorResponse'})
        model = schema['components']['schemas']['RunSummary']
        for name in ('id', 'mode', 'status', 'attempt_count', 'error', 'started_at', 'finished_at'):
            self.assertIn(name, model['required'])
        self.assertFalse(model['additionalProperties'])
        exported = schema['paths']['/api/research-reports/{report_id}/export']['get']['responses']['200']['content']
        self.assertEqual(exported['application/json']['schema'], {'$ref': '#/components/schemas/ResearchReport'})
        self.assertEqual(exported['text/markdown']['schema'], {'type': 'string'})
        monthly_markdown = schema['paths']['/api/monthly-experiments/{experiment_id}/reports/{report_id}/markdown']['get']['responses']['200']['content']
        self.assertEqual(monthly_markdown, {'text/markdown': {'schema': {'type': 'string'}}})
        case_markdown = schema['paths']['/api/research-cases/{case_id}/markdown']['get']['responses']['200']['content']
        self.assertEqual(case_markdown, {'text/markdown': {'schema': {'type': 'string'}}})
        industry_report = schema['paths']['/api/industry-mom-experiments/{experiment_id}/report']['get']
        self.assertEqual(industry_report['responses']['200']['content'],
                         {'text/markdown': {'schema': {'type': 'string'}}})
        self.assertEqual({item['name'] for item in industry_report['parameters'] if item.get('required')},
                         {'experiment_id', 'expected_attempt_id', 'expected_result_digest'})

    def test_workspace_scope_is_stable_private_and_separate(self):
        first = self.client.get('/api/health').json()
        self.assertEqual(first['version'], '0.22.0')
        self.assertEqual(first['database_schema'], int(self.store._read("SELECT value FROM settings WHERE key='schema_version'")[0]['value']))
        self.assertFalse(first['ai_enabled'])
        self.assertEqual(len(first['workspace_id']), 64)
        self.assertNotIn(str(self.root), json_text(first))
        with TestClient(create_app(self.root / 'workspace')) as same:
            self.assertEqual(first['workspace_id'], same.get('/api/health').json()['workspace_id'])
        with TestClient(create_app(self.root / 'another')) as other:
            self.assertNotEqual(first['workspace_id'], other.get('/api/health').json()['workspace_id'])

    def test_generated_discriminators_require_closed_complete_mapping(self):
        valid={'oneOf':[{'$ref':'#/components/schemas/One'},{'$ref':'#/components/schemas/Two'}],
               'discriminator':{'propertyName':'kind','mapping':{'one':'#/components/schemas/One','two':'#/components/schemas/Two'}}}
        validate_vocabulary(valid)
        for change in [{'propertyName':'kind','mapping':{'one':'#/components/schemas/One'}}, {'propertyName':'kind','mapping':{'one':'#/components/schemas/One','two':'#/components/schemas/Two'},'unknown':True}]:
            with self.assertRaises(ValueError):validate_vocabulary({**valid,'discriminator':change})

    def test_real_research_run_revision_review_and_check_contracts(self):
        example = self.client.post('/api/examples/alpha101').json()
        research_path = '/api/researches/' + example['research_id']
        research = self.client.get(research_path).json()
        original_task = research['revisions'][0]['task']
        self.assertEqual(self.client.get('/api/papers/' + research['paper_id']).status_code, 200)
        self.assertEqual(self.client.get('/api/datasets/' + research['dataset_id']).status_code, 200)
        revision = self.client.post(research_path + '/revisions', json={
            'base_revision_id': example['revision_id'], 'task': original_task, 'note': 'Contract acceptance'}).json()
        self.assertEqual(revision['task'], original_task)
        run_response = self.client.post('/api/runs', json={
            'revision_id': revision['id'], 'mode': 'normalized_fixed', 'idempotency_key': 'contract-run'})
        self.assertEqual(run_response.status_code, 201, run_response.text)
        run = run_response.json()
        run_path = '/api/runs/' + run['id']
        pending = self.client.get(run_path).json()
        self.assertIsNone(pending['state'])
        self.assertEqual(pending['attempts'], [])
        claim = self.store.claim('contract-worker')
        self.assertEqual(self.client.get(run_path).json()['status'], 'running')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        self.store.finish(run['id'], 'contract-worker', claim['attempt_id'],
                          'completed' if state['status'] == 'completed' else 'failed')
        finished = self.client.get(run_path)
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertTrue(finished.json()['verification']['verified'])
        review = self.client.post(run_path + '/reviews', json={
            'candidate_id': 'alpha006', 'verdict': 'accepted', 'category': 'implementation',
            'source': 'human', 'note': 'Explicit fixture contract review'}).json()
        case = self.client.post('/api/regression-cases', json={
            'review_id': review['id'], 'expected_status': 'evaluated', 'note': 'Approve numerical reference'}).json()
        check = self.client.post('/api/regression-checks', json={'run_id': run['id'], 'case_ids': [case['id']]})
        self.assertEqual(check.status_code, 201, check.text)
        self.assertEqual(check.json()['outcome'], 'passed')
        self.assertTrue(check.json()['results'][0]['checks'])
        # Strict response projection must preserve the persisted scientific values.
        self.assertEqual(check.json(), self.store.list_checks()[0])
        for path in ('/api/researches', '/api/runs', '/api/papers', '/api/datasets',
                     '/api/regression-cases', '/api/regression-checks', '/api/feedback-summary',
                     run_path + '/events', run_path + '/events/page', run_path + '/artifacts'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
        issue_response = self.client.post('/api/issues', json={
            'review_id': review['id'], 'note': 'Track follow-up', 'source': 'human',
            'disposition': 'accept_limitation', 'idempotency_key': 'contract-issue'})
        self.assertEqual(issue_response.status_code, 201, issue_response.text)
        issue = issue_response.json()
        self.assertEqual(self.client.get('/api/issues/' + issue['id']).json(), issue)
        for resource in ('researches', 'runs', 'reviews', 'regression-cases', 'regression-checks', 'issues'):
            response = self.client.get('/api/catalog/' + resource)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertGreater(len(response.json()['items']), 0)

    def test_dataset_import_contract_from_upload_to_registration_and_invalid_input(self):
        fixture = ROOT / 'tests/fixtures/datasets'
        def upload(key, meta):
            response = self.client.post('/api/dataset-imports',
                files={'file': ('market.csv', (fixture / 'market.csv').read_bytes(), 'text/csv'),
                       'metadata': ('metadata.json', meta, 'application/json')},
                data={'title': 'Contract fixture', 'idempotency_key': key})
            self.assertEqual(response.status_code, 201, response.text)
            return response.json()
        uploaded = upload('valid-upload', (fixture / 'metadata.json').read_bytes())
        self.assertIsNone(uploaded['latest_validation'])
        path = '/api/dataset-imports/' + uploaded['id']
        validated = self.client.post(path + '/validate', json={'idempotency_key': 'validation'})
        self.assertEqual(validated.status_code, 200, validated.text)
        data = validated.json()
        self.assertEqual(data['status'], 'valid')
        registration = self.client.post(path + '/register', json={
            'idempotency_key': 'registration', 'input_digest': data['input_digest'],
            'validation_attempt_id': data['latest_validation']['id'],
            'report_digest': data['latest_validation']['report_digest']})
        self.assertEqual(registration.status_code, 200, registration.text)
        self.assertEqual(registration.json()['status'], 'registered')
        registered_id = registration.json()['registered_dataset_id']
        self.assertEqual(self.client.get('/api/datasets/' + registered_id).json()['registration']['kind'], 'validated_import')
        invalid = upload('invalid-upload', b'not JSON')
        invalid_response = self.client.post('/api/dataset-imports/' + invalid['id'] + '/validate',
                                            json={'idempotency_key': 'invalid-validation'})
        self.assertEqual(invalid_response.status_code, 200, invalid_response.text)
        self.assertEqual(invalid_response.json()['status'], 'invalid')
        self.assertEqual(self.client.get('/api/dataset-imports').status_code, 200)

    def test_missing_fields_unknown_fields_wrong_nulls_and_illegal_status_fail_closed(self):
        example = self.store.seed_example()
        run = self.store.submit_run(example['revision_id'], 'fixed', 'fault-run')
        for mutate in (lambda data: data.pop('attempt_count'),
                       lambda data: data.update(status='mysterious_success'),
                       lambda data: data.update(attempt_count=True),
                       lambda data: data.update(started_at=123),
                       lambda data: data.update(internal_path='/sensitive/private/location')):
            invalid = deepcopy(run)
            mutate(invalid)
            with self.subTest(payload=invalid), patch.object(self.store, 'list_runs', return_value=[invalid]):
                response = self.client.get('/api/runs')
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {'detail': 'Internal response does not match API contract'})
                self.assertNotIn('/sensitive', response.text)
        research = self.store.get_research(example['research_id'])
        del research['revisions'][0]['task']['hypotheses'][0]['attribution']
        with patch.object(self.store, 'get_research', return_value=research):
            self.assertEqual(self.client.get('/api/researches/' + example['research_id']).status_code, 500)

    def test_legacy_check_read_is_explicit_and_new_post_cannot_downgrade(self):
        # Historical v0.2 records contain status-only results. Reading one must
        # neither invent a stronger outcome nor rewrite its immutable payload.
        legacy = {'id': 'legacy-check', 'run_id': 'historical-run', 'attempt_id': 'historical-attempt',
                  'revision_id': 'historical-revision', 'result_digest': 'a' * 64,
                  'created_at': '2026-01-01', 'scope': 'status only', 'passed': True,
                  'results': [{'case_id': 'old-case', 'candidate_id': 'alpha006',
                               'expected_status': 'evaluated', 'actual_status': 'evaluated',
                               'compatible': True, 'passed': True, 'reason': None}]}
        with patch.object(self.store, 'list_checks', return_value=[legacy]):
            response = self.client.get('/api/regression-checks')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), [legacy])
        with patch.object(self.store, 'run_regression_check', return_value=legacy):
            response = self.client.post('/api/regression-checks', json={'run_id': 'run', 'case_ids': ['case']})
            self.assertEqual(response.status_code, 500)

    def test_documented_errors_are_consistent_and_do_not_echo_submitted_input(self):
        for path, body, status in (('/api/runs', {'mode': 'secret-invalid-value'}, 422),
                                   ('/api/runs', {'revision_id': 'missing', 'idempotency_key': 'x'}, 404)):
            response = self.client.post(path, json=body)
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(set(response.json()), {'detail'})
            self.assertIsInstance(response.json()['detail'], str)
            self.assertNotIn('secret-invalid-value', response.text)

    def test_generator_refuses_unimplemented_schema_constraints(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported API schema keywords'):
            validate_vocabulary({'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}})
        with self.assertRaisesRegex(ValueError, 'Unsupported API schema type'):
            validate_vocabulary({'type': ['string', 'null']})

    def test_generated_artifacts_are_deterministic_and_drift_is_detected_without_overwrite(self):
        expected = generate()
        self.assertEqual(generate(), expected)
        for name, content in expected.items():
            self.assertEqual((ROOT / 'frontend/src/generated' / name).read_text(), content)
        directory = self.root / 'generated'
        self.assertEqual(main(['--out', str(directory)]), 0)
        self.assertEqual(main(['--out', str(directory), '--check']), 0)
        target = directory / 'api-contract.ts'
        target.write_text(target.read_text() + '// accidental drift\n')
        self.assertEqual(main(['--out', str(directory), '--check']), 1)
        self.assertTrue(target.read_text().endswith('// accidental drift\n'))


if __name__ == '__main__':
    unittest.main()
