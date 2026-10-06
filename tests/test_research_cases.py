"""Cross-domain exact-result references; no model or market-performance claim."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import tempfile
import unittest

from paper_alpha import eligibility, monthly_workflow, research_protocol
from paper_alpha.server import db
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.monthly_experiments import MonthlyExperiments
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import digest, json_text, read_json
from paper_alpha.workflow import run_task


class ResearchCaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'home'); self.cases = ResearchCases(self.store)

    def study(self, blocked=False):
        scan = eligibility.demo_scan()
        if blocked:
            scan['plan']['minimum_assets'] = 100
            scan['plan_digest'] = digest(scan['plan'])
        return AuthorStudies(self.store).create('Declared aggregate fixture', 'Synthetic aggregate; not raw scanned.',
            scan, None, [], 'blocked' if blocked else 'passed')

    def bind(self, kind, identity, key='bind', **changes):
        preview = self.cases.preview(kind, identity)
        return self.cases.create(**({'title': 'Research context', 'note': 'Engineering fixture only.',
            'source_kind': kind, 'source_id': identity, 'source_digest': preview['source_digest'], 'idempotency_key': key} | changes))

    def monthly(self, paper=False):
        service = MonthlyExperiments(self.store)
        protocol = ResearchProtocols(self.store).create('Declared project protocol', '', research_protocol.presets()[0 if paper else 1]['config'])
        result = service.create(protocol['id'], protocol['digest'],
            {'schema_version': 1, 'start_month': '2025-01', 'end_month': '2025-02', 'cost_bps': 0, 'min_assets': 18}, 'monthly')
        identity = result['experiment_id']; job = service.claim('worker')
        monthly_workflow.run(job['input_path'], job['output_dir'])
        service.finish(identity, 'worker', job['attempt_id'], 'completed')
        return service, identity, job

    def test_author_blocked_and_passed_are_diagnostics_without_execution(self):
        blocked = self.study(True); passed = self.study()
        for study, expected in ((blocked, 'data_insufficient'), (passed, 'rules_unresolved')):
            with self.subTest(expected=expected):
                case = self.bind('author_study', study['id'], expected)
                self.assertEqual(case['context']['state'], expected)
                self.assertNotIn('execute_author_portfolio', case['context']['allowed_actions'])
                self.assertIn('aggregate consistency only', case['context']['data_scope'])
                self.assertIn('execution_ready=false', case['context']['results'][0]['summary'])
                self.assertEqual(self.cases.get(case['id']), case)
                self.assertIn(study['id'], self.cases.markdown(case['id']))
        page = self.cases.list(limit=1)
        self.assertEqual(page['total'], 2); self.assertEqual(len(page['items']), 1)
        self.assertNotIn('context', page['items'][0])

    def test_reviews_are_frozen_subset_with_exact_source_scope(self):
        study = self.study(True); service = AuthorStudies(self.store)
        first = service.review(study['id'], study['digest'], 'data_insufficient', 'Automation diagnostic.', 'automation', 'one')
        case = self.bind('author_study', study['id'])
        service.review(study['id'], study['digest'], 'accepted_with_limits', 'Local declared human only.', 'human', 'two')
        self.assertEqual(self.cases.get(case['id']), case)
        self.assertEqual(case['context']['reviews'][0]['id'], first['id'])
        self.assertEqual(case['context']['reviews'][0]['actor'], 'automation')
        self.assertEqual(len(case['context']['reviews']), 1)
        fresh = self.bind('author_study', study['id'], 'new')
        self.assertNotEqual(fresh['id'], case['id']); self.assertEqual(len(fresh['context']['reviews']), 2)
        self.assertEqual(fresh['context']['state'], 'data_insufficient')

    def test_stale_preview_and_wrong_key_conflict_without_extra_records(self):
        study = self.study(); preview = self.cases.preview('author_study', study['id'])
        AuthorStudies(self.store).review(study['id'], study['digest'], 'rules_unresolved', 'Method absent.', 'automation', 'review')
        with self.assertRaises(ServiceError) as caught:
            self.bind('author_study', study['id'], source_digest=preview['source_digest'])
        self.assertEqual(caught.exception.status, 409); self.assertEqual(self.cases.list()['total'], 0)
        first = self.bind('author_study', study['id'])
        with self.assertRaises(ServiceError):
            self.bind('author_study', study['id'], title='Changed')
        self.assertEqual(self.cases.list()['total'], 1)
        self.assertEqual(self.bind('author_study', study['id']), first)

    def test_concurrent_idempotency_creates_one_context_and_one_receipt(self):
        study = self.study(True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda _: self.bind('author_study', study['id']), range(4)))
        self.assertTrue(all(record == records[0] for record in records))
        self.assertEqual(self.cases.list()['total'], 1)
        self.assertEqual(self.store._read('SELECT COUNT(*) AS n FROM research_case_receipts')[0]['n'], 1)

    def test_case_source_review_and_receipt_tampering_fail_closed(self):
        for target in ('case', 'source', 'review', 'receipt'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                store = Store(Path(temporary)); cases = ResearchCases(store); studies = AuthorStudies(store)
                study = studies.create('Fixture', '', eligibility.demo_scan(), None, [], 'study')
                review = studies.review(study['id'], study['digest'], 'rules_unresolved', 'Pending method.', 'automation', 'review')
                preview = cases.preview('author_study', study['id'])
                args = dict(title='Case', note='', source_kind='author_study', source_id=study['id'], source_digest=preview['source_digest'], idempotency_key='case')
                case = cases.create(**args)
                with db.transaction(store.db_path) as connection:
                    if target == 'case':
                        connection.execute("UPDATE research_cases SET created_at='changed'")
                    elif target == 'source':
                        connection.execute("UPDATE author_studies SET digest=?", ('0' * 64,))
                    elif target == 'review':
                        connection.execute("UPDATE author_study_reviews SET payload=? WHERE id=?", ('{}', review['id']))
                    else:
                        connection.execute("UPDATE research_case_receipts SET request_digest=?", ('0' * 64,))
                with self.assertRaises(ServiceError) as caught:
                    cases.create(**args) if target == 'receipt' else cases.get(case['id'])
                self.assertEqual(caught.exception.status, 409)

    def test_completed_monthly_result_binding_and_file_tamper(self):
        service, identity, job = self.monthly()
        detail = service.get(identity)
        case = self.bind('monthly_experiment', identity)
        self.assertEqual(case['context']['state'], 'ready_for_review')
        result = case['context']['results'][0]
        self.assertEqual(result['digest'], detail['review_target']['result_digest'])
        self.assertEqual(result['payload_json'], json_text(detail['result']))
        self.assertIn('controlled_fixture', case['context']['data_scope'])
        self.assertEqual([e['origin'] for e in case['context']['evidence']], ['paper', 'author_code', 'project'])
        self.assertEqual(case['context']['evidence'][-1]['verification'], 'project_declaration')
        service.create_review(identity, **detail['review_target'], verdict='accepted', note='Automated fixture.', source='automation', idempotency_key='review')
        self.assertEqual(self.cases.get(case['id']), case)
        (Path(job['output_dir']) / 'result.json').write_text('{}')
        with self.assertRaises(ServiceError) as caught:
            self.cases.get(case['id'])
        self.assertEqual(caught.exception.status, 409)

    def test_unresolved_monthly_rules_and_noncompleted_run(self):
        _, identity, _ = self.monthly(paper=True)
        case = self.bind('monthly_experiment', identity)
        self.assertEqual(case['context']['state'], 'rules_unresolved')
        seed = self.store.seed_example()
        queued = self.store.submit_run(seed['revision_id'], 'agent', 'queued')
        with self.assertRaises(ServiceError) as caught:
            self.cases.preview('daily_run', queued['id'])
        self.assertEqual(caught.exception.status, 409)

    def test_real_daily_engine_result_evidence_and_attempt_binding(self):
        seed = self.store.seed_example()
        run = self.store.submit_run(seed['revision_id'], 'agent', 'daily')
        job = self.store.claim('daily-worker')
        state = run_task(job['task_path'], job['output_dir'], mode='agent')
        self.assertEqual(state['status'], 'completed')
        self.store.finish(run['id'], 'daily-worker', job['attempt_id'], 'completed')
        case = self.bind('daily_run', run['id'])
        self.assertEqual(case['context']['state'], 'ready_for_review')
        self.assertTrue(all(e['verification'] == 'literal_quote_verified' for e in case['context']['evidence']))
        self.assertEqual(case['context']['results'][0]['id'], job['attempt_id'])
        manifest = read_json(Path(job['output_dir']) / 'manifest.json')
        self.assertTrue(manifest['signature']['code'])
        provenance = {item['label']: item['value'] for item in case['context']['provenance']}
        self.assertEqual(provenance['code_digest'], digest(manifest['signature']['code']))
        self.assertNotEqual(provenance['code_digest'], digest({}))
        self.assertTrue(case['context']['definitions'])
        self.assertNotIn(str(self.store.root), json_text(case))
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE runs SET attempt_id=NULL WHERE id=?', (run['id'],))
        with self.assertRaises(ServiceError):
            self.cases.get(case['id'])

    def test_schema12_upgrade_preserves_legacy_records_and_backup_roundtrip(self):
        study = self.study(True)
        before = self.store._read('SELECT * FROM author_studies')
        # Simulate the exact additive migration boundary before any v17 records.
        # All later schemas must be removed, including tables whose names do
        # not start with research_. Production migration stays strict.
        from paper_alpha.server.research_cases import SCHEMA as CASE_SCHEMA
        from paper_alpha.server.research_tools import SCHEMA as TOOL_SCHEMA
        from paper_alpha.server.research_guard import SCHEMA as GUARD_SCHEMA
        from paper_alpha.server.research_jobs import SCHEMA as JOB_SCHEMA
        from paper_alpha.server.research_claims import SCHEMA as CLAIM_SCHEMA
        from paper_alpha.server.observation_identity import SCHEMA as OBSERVATION_SCHEMA
        from paper_alpha.server.domain_research_jobs import SCHEMA as DOMAIN_SCHEMA
        from paper_alpha.server.semantic_annotations import SCHEMA as SEMANTIC_SCHEMA
        from tests.v020_schema_helpers import SCHEMA as V020_SCHEMA
        later_tables = re.findall(r'CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)',
            CASE_SCHEMA + TOOL_SCHEMA + GUARD_SCHEMA + JOB_SCHEMA + CLAIM_SCHEMA
            + OBSERVATION_SCHEMA + DOMAIN_SCHEMA + SEMANTIC_SCHEMA + V020_SCHEMA)
        with db.transaction(self.store.db_path) as connection:
            current_tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            from paper_alpha.server.industry_mom_storage_schema import SCHEMA as INDUSTRY_MOM_SCHEMA
            self.assertEqual(len(later_tables), 29 + len(re.findall(r'CREATE TABLE\s+(\w+)', INDUSTRY_MOM_SCHEMA)))
            self.assertTrue(set(later_tables).issubset(current_tables))
            for table in reversed(later_tables):
                connection.execute('DROP TABLE ' + table)
            remaining_tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(remaining_tables, current_tables - set(later_tables))
            connection.execute("UPDATE settings SET value='12' WHERE key='schema_version'")
            connection.execute('DELETE FROM schema_migrations WHERE version>=13')
        self.assertEqual(self.store._read('SELECT * FROM author_studies'), before)
        restored_store = Store(self.store.root)
        self.assertEqual(restored_store._read('SELECT * FROM author_studies'), before)
        self.assertEqual(restored_store._read("SELECT value FROM settings WHERE key='schema_version'")[0]['value'], str(db.SCHEMA_VERSION))
        case = self.bind('author_study', study['id'])
        create_backup(self.store.root, self.root / 'backup')
        restore_backup(self.root / 'backup', self.root / 'restored')
        restored = ResearchCases(Store(self.root / 'restored'))
        self.assertEqual(restored.get(case['id']), case)
        self.assertEqual(self.store._read('SELECT * FROM author_studies'), before)

    def test_invalid_inputs_are_rejected(self):
        for kind, identity in (('portfolio', 'x'), ('author_study', 5)):
            with self.assertRaises(ServiceError):
                self.cases.preview(kind, identity)
        for value in (True, 0, 101):
            with self.assertRaises(ServiceError):
                self.cases.list(limit=value)
        with self.assertRaises(ServiceError):
            self.cases.get('../../secret')
