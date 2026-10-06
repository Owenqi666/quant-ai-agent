"""HTTP integration: exact targets, declaration snapshots and blocked bindings."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
import re
import subprocess
import sys
import json

from fastapi.testclient import TestClient
from paper_alpha import eligibility
from paper_alpha.research_protocol import presets
from paper_alpha.server.api import create_app
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.semantic_evaluation import verify_bundle
from paper_alpha.server import db
from paper_alpha.server.backup import create_backup
from tests.v020_schema_helpers import SCHEMA as V020_SCHEMA

class V020ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = create_app(self.root / 'workspace')
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.store = self.app.state.store

    def post(self, path, body, status=201):
        response = self.client.post('/api' + path, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def dimensions(self):
        return {key: {'outcome': 'not_assessed', 'reason': 'Isolated HTTP automation fixture, no human judgment.'} for key in DIMENSIONS}

    def study(self):
        return AuthorStudies(self.store).create('HTTP source fixture', 'Synthetic aggregate counts, no market performance.', eligibility.demo_scan(), None, [], 'http-study')

    def test_exact_claim_target_query_supports_slash_and_no_approval_inheritance(self):
        study = self.study(); cases = ResearchCases(self.store)
        context = cases.preview('author_study', study['id'])
        case = cases.create('HTTP claim source', '', 'author_study', study['id'], context['source_digest'], 'http-case')
        metric = case['context']['results'][0]
        draft = {'id': 'count/with slash', 'kind': 'metric', 'attribution': 'project_convention', 'text': 'Untrusted narrative incorrectly says 999999.',
            'evidence_ids': [], 'metric_references': [{'case_id': case['id'], 'case_digest': case['digest'], 'result_id': metric['id'], 'result_digest': metric['digest'], 'pointer': '/summary/min_selected'}]}
        batch = ResearchClaims(self.store).create(case['id'], case['digest'], [draft], 'http-claims')
        target_response = self.client.get('/api/claim-review-targets/' + batch['id'], params={'claim_id': draft['id']})
        self.assertEqual(target_response.status_code, 200, target_response.text)
        target = target_response.json()
        self.assertNotIn('999999', target['claim']['authoritative_display'])
        request = {'claims_id': batch['id'], 'claim_id': draft['id'], 'expected_target_digest': target['target_digest'],
            'source': 'automation', 'reviewer': 'HTTP automation fixture', 'confirmed_at': datetime.now(timezone.utc).isoformat(),
            'dimensions': self.dimensions(), 'supersedes_id': None, 'expected_supersedes_digest': None}
        self.post('/claim-reviews/preview', request, 200)
        review = self.post('/claim-reviews', {**request, 'idempotency_key': 'http-review'})
        self.assertEqual(review, self.post('/claim-reviews', {**request, 'idempotency_key': 'http-review'}))
        self.assertEqual(self.client.get('/api/claim-reviews/' + review['id'] + '/export').json(), review)
        status = self.client.get('/api/claim-reviews/status', params={'claims_id': batch['id'], 'claim_id': draft['id']}).json()
        self.assertEqual(status['human_declared_status'], 'pending'); self.assertEqual(status['human_records'], 0)
        self.assertEqual(ResearchClaims(self.store).get(batch['id']), batch)
        self.post('/claim-reviews', {**request, 'expected_target_digest': '0' * 64, 'idempotency_key': 'stale-review'}, 409)
        self.post('/claim-reviews', {**request, 'approved': True, 'idempotency_key': 'injected-review'}, 422)

    def test_pending_reference_comparison_and_export_keep_quality_null(self):
        material = self.client.get('/api/semantic-materials').json()
        request = {'material_sha256': material['material_sha256'], 'case_ids': ['alpha101_formula']}
        preview = self.post('/semantic-evaluation-sets/preview', request, 200)
        reference = self.post('/semantic-evaluation-sets', {**request, 'expected_active_annotations': preview['active_human_annotations'], 'idempotency_key': 'http-reference'})
        comparison = self.post('/semantic-evaluation-comparisons', {'set_id': reference['id'], 'set_digest': reference['digest'],
            'source': 'automation', 'reviewer': 'HTTP automation fixture', 'declared_at': datetime.now(timezone.utc).isoformat(),
            'execution_reference': 'Declared HTTP fixture; execution reference not authenticated.', 'case_declarations': [{'case_id': 'alpha101_formula', 'dimensions': self.dimensions()}], 'idempotency_key': 'http-compare'})
        self.assertIsNone(comparison['summary']['semantic_quality_score'])
        self.assertIsNone(comparison['summary']['declaration_agreement_rate'])
        self.assertEqual(comparison['summary']['total_dimensions'], 5)
        response = self.client.get('/api/semantic-evaluation-sets/' + reference['id'] + '/export')
        self.assertEqual(response.status_code, 200, response.text)
        bundle = response.json(); verify_bundle(bundle)
        self.assertFalse(bundle['binary_sources_included'])
        self.assertEqual(self.client.get('/api/semantic-evaluation-sets/' + reference['id'] + '/comparisons').json()['total'], 1)
        self.post('/semantic-evaluation-comparisons', {**comparison, 'idempotency_key': 'metric-injection'}, 422)

    def test_binding_prepare_derives_exact_digests_and_never_executes(self):
        study = self.study()
        paper = self.store.add_paper((Path(__file__).resolve().parents[1] / 'examples/alpha101/paper.pdf').read_bytes(), 'Bundled contract fixture')
        protocol = self.post('/research-protocols', {'title': 'HTTP paper-mode rule fixture', 'note': '', 'config': presets()[0]['config'], 'parent_id': None})
        prepared = self.post('/research-bindings/prepare', {'paper_id': paper['id'], 'protocol_id': protocol['id'], 'study_id': study['id'],
            'source_scope': 'controlled_contract_fixture', 'title': 'HTTP binding fixture', 'note': 'Unrelated fixture paper, no GJS semantic claim.'}, 200)
        self.assertFalse(prepared['context']['execution_ready'])
        self.assertFalse(prepared['context']['raw_source_reverified'])
        binding = self.post('/research-bindings', {**prepared['request'], 'preview_digest': prepared['preview_digest'], 'idempotency_key': 'http-binding'})
        self.assertEqual(self.client.get('/api/research-bindings/' + binding['id'] + '/export').json()['binding'], binding)
        self.assertEqual(self.client.get('/api/research-bindings/' + binding['id'] + '/markdown').status_code, 200)
        self.assertEqual(self.client.get('/api/monthly-experiments').json()['total'], 0)
        self.post('/research-bindings', {**prepared['request'], 'preview_digest': prepared['preview_digest'], 'idempotency_key': 'false-source', 'raw_source_reverified': True}, 422)
        self.post('/research-bindings/prepare', {**{'paper_id': paper['id'], 'protocol_id': protocol['id'], 'study_id': study['id'], 'title': 'Wrong paper', 'note': ''}, 'source_scope': 'author_paper'}, 422)

    def test_http_rejects_arbitrary_paths_duplicate_json_and_untyped_quality(self):
        material = self.client.get('/api/semantic-materials').json()
        body = {'material_sha256': material['material_sha256'], 'case_ids': ['alpha101_formula'], 'path': '/etc/passwd'}
        self.post('/semantic-evaluation-sets/preview', body, 422)
        response = self.client.post('/api/semantic-evaluation-sets/preview', content='{"case_ids":[],"case_ids":[]}', headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/api/health').json()['database_schema'], db.SCHEMA_VERSION)

    def test_schema15_backup_clone_preserves_previous_business_and_adds_empty_tables(self):
        study = self.study()
        with db.transaction(self.store.db_path) as connection:
            for name in reversed(re.findall(r'CREATE TABLE\s+(\w+)', V020_SCHEMA)):
                connection.execute('DROP TABLE ' + name)
            connection.execute("UPDATE settings SET value='15' WHERE key='schema_version'")
            connection.execute('DELETE FROM schema_migrations WHERE version>=16')
        backup = self.root / 'schema15-backup'
        create_backup(self.store.root, backup)
        out = self.root / 'clone-upgrade'
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, str(root / 'scripts/check_v020_upgrade.py'), '--backup', str(backup), '--out', str(out)]
        completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads((out / 'result.json').read_text())
        self.assertTrue(report['passed']); self.assertEqual(set(report['new_tables']), set(re.findall(r'CREATE TABLE\s+(\w+)', V020_SCHEMA)))
        self.assertTrue(report['new_tables_empty']); self.assertTrue(report['historical_files_preserved'])
        self.assertEqual(self.store._read("SELECT value FROM settings WHERE key='schema_version'")[0]['value'], '15')

if __name__ == '__main__':
    unittest.main()
