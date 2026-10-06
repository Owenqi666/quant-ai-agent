"""HTTP separation between human controls and a budgeted tool capability."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from paper_alpha.eligibility import demo_scan
from paper_alpha.server.api import create_app
from paper_alpha.storage import digest


class ResearchCasesApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(Path(self.temp.name)))
        self.addCleanup(self.client.close)
        scan = demo_scan(); scan['plan']['minimum_assets'] = 100
        scan['plan_digest'] = digest(scan['plan'])
        self.study = self.client.post('/api/author-studies', json={
            'title': 'HTTP synthetic blocked source', 'note': 'No raw source or human claim.',
            'scan': scan, 'parent_review_id': None, 'author_panel_ids': [], 'idempotency_key': 'study'}).json()
        preview = self.client.get('/api/research-cases/preview', params={'source_kind': 'author_study', 'source_id': self.study['id']})
        self.assertEqual(preview.status_code, 200)
        self.body = {'title': 'HTTP bounded research', 'note': '', 'source_kind': 'author_study',
                     'source_id': self.study['id'], 'source_digest': preview.json()['source_digest'], 'idempotency_key': 'case'}
        response = self.client.post('/api/research-cases', json=self.body)
        self.assertEqual(response.status_code, 201, response.text)
        self.case = response.json()

    def session(self, **changes):
        body = {'case_id': self.case['id'], 'case_digest': self.case['digest'],
                'budget': {'max_calls': 1, 'max_errors': 1, 'max_seconds': 30}, 'idempotency_key': 'session'} | changes
        response = self.client.post('/api/research-tool-sessions', json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_bound_result_replay_and_machine_readable_budget(self):
        self.assertEqual(self.client.post('/api/research-cases', json=self.body).json(), self.case)
        self.assertEqual(self.case['context']['state'], 'data_insufficient')
        session = self.session()
        route = '/api/research-tool-sessions/' + session['id'] + '/calls'
        body = {'tool': 'read_result', 'arguments': {}, 'idempotency_key': 'read'}
        response = self.client.post(route, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['response']['ok'])
        self.assertEqual(self.client.post(route, json=body).json(), response.json())
        rejected = self.client.post(route, json={**body, 'idempotency_key': 'another'})
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.json()['code'], 'BUDGET_EXHAUSTED')
        self.assertFalse(rejected.json()['retryable'])
        saved = self.client.get('/api/research-tool-sessions/' + session['id']).json()
        self.assertEqual(saved['usage']['calls'], 1)
        self.assertEqual(saved['status'], 'exhausted')

    def test_no_review_authority_no_injected_results_and_strict_http_shape(self):
        session = self.session(); route = '/api/research-tool-sessions/' + session['id'] + '/calls'
        malformed = self.client.post(route, json={'tool': 'read_result', 'arguments': {'metrics': {'sharpe': 10}}, 'idempotency_key': 'invalid'})
        self.assertEqual(malformed.status_code, 422)
        denied = self.client.post(route, json={'tool': 'write_human_review', 'arguments': {}, 'idempotency_key': 'denied'})
        self.assertEqual(denied.status_code, 200)
        self.assertEqual(denied.json()['response']['error']['code'], 'TOOL_DENIED')
        self.assertEqual(self.client.get('/api/author-studies/' + self.study['id'] + '/reviews').json(), {'items': []})
        self.assertEqual(self.client.get('/api/author-studies/' + self.study['id']).json(), self.study)
        capabilities = self.client.get('/api/research-tools/capabilities').json()
        self.assertFalse(capabilities['provider_connected'])
        self.assertEqual({item['authority'] for item in capabilities['tools']}, {'read_only', 'automation_draft'})

    def test_report_filters_and_legacy_schema_are_preserved(self):
        self.session()
        page = self.client.get('/api/research-tool-sessions', params={'case_id': self.case['id']}).json()
        self.assertEqual(page['total'], 1)
        self.assertEqual(self.client.get('/api/research-cases').json()['total'], 1)
        report = self.client.get('/api/research-cases/' + self.case['id'] + '/markdown')
        self.assertEqual(report.status_code, 200)
        self.assertIn(self.study['id'], report.text)
        schema = self.client.get('/openapi.json').json()
        legacy = schema['paths']['/api/regression-cases']['post']['requestBody']['content']['application/json']['schema']['$ref'].split('/')[-1]
        self.assertIn('review_id', schema['components']['schemas'][legacy]['properties'])
        self.assertNotIn('source_kind', schema['components']['schemas'][legacy]['properties'])
