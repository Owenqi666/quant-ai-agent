"""Aggregate study/review/revision correctness in isolated SQLite workspaces."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from paper_alpha import eligibility
from paper_alpha.author_archive_contract import source_metadata, ADAPTER_VERSION
from paper_alpha.server import db, author_studies
from paper_alpha.server.api import create_app
from paper_alpha.server.author_panels import AuthorPanels, _metadata_digest
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import digest, json_text


def revised(scan=None, **plan):
    value = deepcopy(scan or eligibility.demo_scan())
    value['plan'].update(plan)
    value['plan_digest'] = digest(value['plan'])
    return value


def panel_input(filename='IntnlData.mat', month='1993-03'):
    year, number = map(int, month.split('-')); last = year * 12 + number - 1
    months = [f'{index // 12:04d}-{index % 12 + 1:02d}' for index in range(last - 12, last + 1)]
    return {'schema_version': 1, 'kind': 'author_perturbed_monthly_panel', 'adapter_version': ADAPTER_VERSION,
            'source': source_metadata(filename), 'selection': {'target_month': month, 'row_offset': 0, 'row_count': 1},
            'months': months, 'source_observation_dates': [None] * 13 if filename == 'IntnlData.mat' else [value + '-28' for value in months],
            'rows': [{'source_row': 1, 'asset': filename + ':row:1', 'country': 'AE' if filename == 'IntnlData.mat' else None,
                      'returns': [0.0] * 13, 'return_states': ['value'] * 13, 'dgw': 0.0, 'dgw_state': 'value',
                      'market_cap': 1.0, 'market_cap_state': 'value'}]}


class StudyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.store = Store(self.root / 'home')
        self.studies = AuthorStudies(self.store)

    def create(self, **changes):
        return self.studies.create(**({'title': 'Eligibility screen', 'note': 'Synthetic aggregate fixture only.',
            'scan': eligibility.demo_scan(), 'parent_review_id': None, 'author_panel_ids': [], 'idempotency_key': 'create'} | changes))

    def review(self, study, **changes):
        return self.studies.review(study['id'], **({'study_digest': study['digest'], 'decision': 'data_insufficient',
            'note': 'Fixture automation; no authenticated human judgment.', 'actor': 'automation', 'idempotency_key': 'review'} | changes))

    def panel(self, **changes):
        value = panel_input(**changes)
        return AuthorPanels(self.store).create('Raw-row fixture', 'Synthetic panel for relation tests', value, digest(value))

    def rows(self, table):
        return self.store._read('SELECT * FROM ' + table)

    def test_import_replay_report_paging_and_exact_text_preserved(self):
        first = self.create(title='  Screen  ')
        self.assertEqual(self.create(title='  Screen  '), first)
        self.assertEqual(self.create(title='  Screen  ', idempotency_key='new'), first)
        self.assertEqual(self.studies.get(first['id']), first)
        self.assertEqual(self.studies.markdown(first['id']), eligibility.render_report(eligibility.evaluate(eligibility.demo_scan())))
        self.assertEqual(self.studies.reviews(first['id']), {'items': []})
        self.assertFalse(first['raw_source_reverified']); self.assertFalse(first['result']['execution_ready'])
        self.assertEqual(first['verification_scope'], 'aggregate_consistency_only')
        self.assertEqual(first['changes'], [])
        second = self.create(title='Second', idempotency_key='second')
        pages = [self.studies.list(limit=1, offset=offset) for offset in (0, 1)]
        self.assertEqual({page['items'][0]['id'] for page in pages}, {first['id'], second['id']})
        self.assertTrue(all(page['total'] == 2 for page in pages))
        self.assertNotIn('scan', pages[0]['items'][0])

    def test_same_key_different_request_conflicts_without_new_record(self):
        first = self.create()
        for kwargs in ({'title': 'changed'}, {'note': 'changed'}, {'scan': revised(minimum_assets=31)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ServiceError) as raised:
                self.create(**kwargs)
            self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.studies.list()['total'], 1)
        self.assertEqual(self.studies.get(first['id']), first)

    def test_exact_review_binding_actor_append_only_and_replay(self):
        study = self.create()
        first = self.review(study)
        self.assertEqual(self.review(study), first)
        self.assertEqual(self.review(study, idempotency_key='another'), first)
        other = self.review(study, decision='accepted_with_limits', note='Declared local human review', actor='human', idempotency_key='human')
        self.assertEqual(self.studies.reviews(study['id'])['items'], [first, other])
        self.assertEqual(self.studies.get(study['id']), study)
        with self.assertRaises(ServiceError) as raised:
            self.review(study, study_digest='0' * 64, idempotency_key='stale')
        self.assertEqual(raised.exception.status, 409)
        with self.assertRaises(ServiceError) as raised:
            self.review(study, note='Different request')
        self.assertEqual(raised.exception.status, 409)

    def test_revision_changes_and_parent_review_do_not_copy_approval(self):
        parent = self.create(); review = self.review(parent, decision='accepted_with_limits')
        child = self.create(scan=revised(minimum_assets=100), parent_review_id=review['id'], idempotency_key='child')
        self.assertEqual(child['parent_study_id'], parent['id'])
        self.assertEqual(child['parent_review_id'], review['id'])
        self.assertEqual([change['field'] for change in child['changes']], ['plan.minimum_assets', 'input_digest'])
        self.assertEqual(child['changes'][0], {'field': 'plan.minimum_assets', 'before': json_text(30), 'after': json_text(100)})
        self.assertEqual(child['result']['summary']['status'], 'screen_blocked')
        self.assertEqual(self.studies.reviews(child['id']), {'items': []})
        self.assertEqual(self.studies.reviews(parent['id']), {'items': [review]})
        self.assertEqual(self.studies.get(child['id']), child)
        with self.assertRaises(ServiceError) as raised:
            self.create(parent_review_id=review['id'], title='Title alone is not revision', idempotency_key='unchanged')
        self.assertEqual(raised.exception.status, 422)

    def test_verified_linked_panels_match_source_and_development_window(self):
        matching = self.panel(); outside = self.panel(month='1993-05'); wrong = self.panel(filename='USData.mat')
        study = self.create(author_panel_ids=[matching['id']])
        self.assertEqual(self.studies.get(study['id']), study)
        for identity in (outside['id'], wrong['id'], 'author_panel_' + '0' * 64):
            with self.subTest(identity=identity), self.assertRaises(ServiceError) as raised:
                self.create(author_panel_ids=[identity], idempotency_key=identity)
            self.assertEqual(raised.exception.status, 409)
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE author_panels SET created_at='1990-01-01T00:00:00+00:00' WHERE id=?", (matching['id'],))
        with self.assertRaises(ServiceError) as raised:
            self.studies.get(study['id'])
        self.assertEqual(raised.exception.status, 409)

    def test_source_revision_requires_replacing_incompatible_panel_links(self):
        panel = self.panel(); parent = self.create(author_panel_ids=[panel['id']]); review = self.review(parent)
        scan = eligibility.demo_scan(); scan['source'] = source_metadata('USData.mat'); scan['plan']['source_file'] = 'USData.mat'
        scan['plan_digest'] = digest(scan['plan'])
        for month in scan['months']:
            month['patterns'][0] = scan['source']['assets'] - 60; month['momentum_missing'] = scan['source']['assets'] - 60
        with self.assertRaises(ServiceError):
            self.create(scan=scan, parent_review_id=review['id'], author_panel_ids=[panel['id']], idempotency_key='wrong-link')
        child = self.create(scan=scan, parent_review_id=review['id'], idempotency_key='source-change')
        self.assertIn('source', [item['field'] for item in child['changes']])
        self.assertEqual(self.studies.get(child['id']), child)

    def test_input_validation_bounds_before_mutation(self):
        bad_scan = eligibility.demo_scan(); bad_scan['months'][0]['patterns'][0] = True
        cases = [{'title': ' '}, {'title': '\ud800'}, {'note': 'a' * 4001}, {'idempotency_key': ''},
                 {'scan': bad_scan}, {'parent_review_id': 'bad'}, {'author_panel_ids': [{}]},
                 {'author_panel_ids': ['author_panel_' + '0' * 64] * 2}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ServiceError) as raised:
                self.create(**value)
            self.assertEqual(raised.exception.status, 422)
        for value in ({'limit': True}, {'limit': 101}, {'offset': True}, {'offset': 100001}):
            with self.assertRaises(ServiceError) as raised: self.studies.list(**value)
            self.assertEqual(raised.exception.status, 422)
        self.assertEqual(self.studies.list()['total'], 0)

    def test_concurrent_import_and_review_have_one_body_and_receipt(self):
        def parallel(call):
            gate = threading.Barrier(4)
            def run(_): gate.wait(timeout=10); return call()
            with ThreadPoolExecutor(max_workers=4) as executor: return list(executor.map(run, range(4)))
        values = parallel(self.create)
        self.assertTrue(all(value == values[0] for value in values))
        reviews = parallel(lambda: self.review(values[0]))
        self.assertTrue(all(value == reviews[0] for value in reviews))
        self.assertEqual(len(self.rows('author_studies')), 1)
        self.assertEqual(len(self.rows('author_study_reviews')), 1)
        self.assertEqual(len(self.rows('author_study_receipts')), 2)

    def test_failed_receipt_insert_rolls_back_study_and_review(self):
        with db.transaction(self.store.db_path) as connection:
            connection.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON author_study_receipts BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.create()
        self.assertEqual(self.studies.list()['total'], 0)
        with db.transaction(self.store.db_path) as connection: connection.execute('DROP TRIGGER fail_receipt')
        study = self.create()
        with db.transaction(self.store.db_path) as connection:
            connection.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON author_study_receipts BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.review(study)
        self.assertEqual(self.studies.reviews(study['id']), {'items': []})

    def test_metadata_tamper_blocks_detail_list_and_replay_without_write(self):
        study = self.create()
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE author_studies SET created_at='1990-01-01T00:00:00+00:00'")
            before = '\n'.join(connection.iterdump())
        for call in (lambda: self.studies.get(study['id']), self.studies.list, self.create):
            with self.assertRaises(ServiceError) as raised: call()
            self.assertEqual(raised.exception.status, 409)
        with closing(db.connect(self.store.db_path)) as connection:
            self.assertEqual('\n'.join(connection.iterdump()), before)

    def test_rehashed_false_result_and_changes_fail_semantic_verification(self):
        parent = self.create(); review = self.review(parent)
        child = self.create(scan=revised(minimum_assets=31), parent_review_id=review['id'], idempotency_key='child')
        for mutate in (lambda body: body['result']['summary'].update(min_selected=999), lambda body: body.update(changes=[])):
            # Use an independent malformed root-like row after removing receipts;
            # all parent bindings stay valid, so failure must be semantic.
            original = next(row for row in self.rows('author_studies') if row['id'] == child['id'])
            body = json.loads(original['payload']); mutate(body)
            fingerprint = digest(body); identity = 'author_study_' + fingerprint
            with db.transaction(self.store.db_path) as connection:
                connection.execute('DELETE FROM author_study_receipts WHERE study_id=?', (child['id'],))
                connection.execute('UPDATE author_studies SET id=?,digest=?,payload=?,metadata_digest=? WHERE id=?',
                    (identity, fingerprint, json_text(body), _metadata_digest(identity, fingerprint, original['created_at']), child['id']))
            with self.assertRaises(ServiceError) as raised: self.studies.get(identity)
            self.assertEqual(raised.exception.status, 409)
            with db.transaction(self.store.db_path) as connection:
                connection.execute('UPDATE author_studies SET id=?,digest=?,payload=?,metadata_digest=? WHERE id=?',
                    (child['id'], original['digest'], original['payload'], original['metadata_digest'], identity))

    def test_parent_review_tamper_blocks_existing_child_and_new_revision(self):
        parent = self.create(); review = self.review(parent)
        child = self.create(scan=revised(minimum_assets=31), parent_review_id=review['id'], idempotency_key='child')
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE author_study_reviews SET created_at='1990-01-01T00:00:00+00:00'")
        for call in (lambda: self.studies.get(child['id']), lambda: self.studies.reviews(parent['id']),
                     lambda: self.create(scan=revised(minimum_assets=32), parent_review_id=review['id'], idempotency_key='new')):
            with self.assertRaises(ServiceError) as raised: call()
            self.assertEqual(raised.exception.status, 409)

    def test_rehashed_review_still_requires_parent_study_digest(self):
        study = self.create(); review = self.review(study)
        row = self.rows('author_study_reviews')[0]; body = json.loads(row['payload']); body['study_digest'] = '0' * 64
        fingerprint = digest(body); identity = 'author_review_' + fingerprint
        with db.transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM author_study_receipts WHERE review_id=?', (review['id'],))
            connection.execute('UPDATE author_study_reviews SET id=?,digest=?,payload=?,metadata_digest=? WHERE id=?',
                (identity, fingerprint, json_text(body), _metadata_digest(identity, fingerprint, row['created_at']), review['id']))
        for call in (lambda: self.studies.reviews(study['id']),
                     lambda: self.create(scan=revised(minimum_assets=31), parent_review_id=identity, idempotency_key='child')):
            with self.assertRaises(ServiceError) as raised: call()
            self.assertEqual(raised.exception.status, 409)

    def test_receipt_tamper_and_missing_resource_do_not_recreate_history(self):
        study = self.create()
        with db.transaction(self.store.db_path) as connection: connection.execute("UPDATE author_study_receipts SET resource_digest='changed'")
        with self.assertRaises(ServiceError) as raised: self.create()
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.studies.get(study['id']), study)
        with closing(db.connect(self.store.db_path)) as connection:
            connection.execute('PRAGMA foreign_keys=OFF'); connection.execute('DELETE FROM author_studies')
        with self.assertRaises(ServiceError) as raised: self.create()
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.rows('author_studies'), [])

    def test_dependency_change_between_read_and_write_is_fenced(self):
        parent = self.create(); review = self.review(parent)
        original = author_studies._prepared
        def mutate(*args, **kwargs):
            prepared = original(*args, **kwargs)
            with db.transaction(self.store.db_path) as connection:
                connection.execute("UPDATE author_study_reviews SET created_at='1990-01-01T00:00:00+00:00'")
            return prepared
        with patch.object(author_studies, '_prepared', side_effect=mutate), self.assertRaises(ServiceError) as raised:
            self.create(scan=revised(minimum_assets=31), parent_review_id=review['id'], idempotency_key='child')
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(len(self.rows('author_studies')), 1)

    def test_computation_and_link_verification_do_not_hold_writer_lock(self):
        original = eligibility.evaluate
        def compute(scan):
            with db.transaction(self.store.db_path) as connection:
                connection.execute("INSERT OR REPLACE INTO settings VALUES ('study-test','outside-writer')")
            return original(scan)
        with patch.object(eligibility, 'evaluate', side_effect=compute):
            parent = self.create(); self.review(parent)

    def test_ancestry_cap_and_review_count_cap_fail_closed(self):
        parent = self.create()
        with patch.object(author_studies, 'MAX_ANCESTRY', 3):
            for index in (1, 2):
                review = self.review(parent, idempotency_key='review-' + str(index))
                parent = self.create(scan=revised(minimum_assets=30 + index), parent_review_id=review['id'], idempotency_key='child-' + str(index))
            self.assertEqual(self.studies.get(parent['id']), parent)
            review = self.review(parent, idempotency_key='review-3')
            with self.assertRaises(ServiceError) as raised:
                self.create(scan=revised(minimum_assets=33), parent_review_id=review['id'], idempotency_key='fourth')
            self.assertEqual(raised.exception.status, 413)
        with patch.object(author_studies, 'MAX_REVIEWS', 1):
            with self.assertRaises(ServiceError) as raised: self.review(parent, note='Second review', idempotency_key='another')
            self.assertEqual(raised.exception.status, 413)
        with patch.object(author_studies, 'MAX_REVIEWS', 0):
            with self.assertRaises(ServiceError) as raised: self.studies.reviews(parent['id'])
            self.assertEqual(raised.exception.status, 413)

    def test_backup_restore_preserves_links_reviews_and_exact_receipts(self):
        panel = self.panel(); parent = self.create(author_panel_ids=[panel['id']]); review = self.review(parent)
        child = self.create(scan=revised(minimum_assets=31), parent_review_id=review['id'], idempotency_key='child')
        create_backup(self.store.root, self.root / 'backup'); restore_backup(self.root / 'backup', self.root / 'restored')
        self.studies = AuthorStudies(Store(self.root / 'restored'))
        self.assertEqual(self.studies.get(child['id']), child)
        self.assertEqual(self.create(author_panel_ids=[panel['id']]), parent)
        self.assertEqual(self.review(parent), review)


class StudyHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name).resolve() / 'home')
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.body = {'title': 'Synthetic source eligibility', 'note': '', 'scan': eligibility.demo_scan(),
                     'parent_review_id': None, 'author_panel_ids': [], 'idempotency_key': 'http'}

    def test_http_import_detail_report_review_revision_flow(self):
        response = self.client.post('/api/author-studies', json=self.body)
        self.assertEqual(response.status_code, 201, response.text)
        parent = response.json(); path = '/api/author-studies/' + parent['id']
        self.assertEqual(self.client.post('/api/author-studies', json=self.body).json(), parent)
        self.assertEqual(self.client.get(path).json(), parent)
        self.assertEqual(self.client.get(path + '/markdown').text, eligibility.render_report(parent['result']))
        self.assertEqual(self.client.get(path + '/reviews').json(), {'items': []})
        review_body = {'study_digest': parent['digest'], 'decision': 'rules_unresolved', 'actor': 'automation',
                       'note': 'Deliberately synthetic HTTP test', 'idempotency_key': 'review'}
        review = self.client.post(path + '/reviews', json=review_body)
        self.assertEqual(review.status_code, 201, review.text)
        self.assertEqual(self.client.post(path + '/reviews', json=review_body).json(), review.json())
        child = self.client.post('/api/author-studies', json=self.body | {'scan': revised(minimum_assets=31),
                    'parent_review_id': review.json()['id'], 'idempotency_key': 'child'})
        self.assertEqual(child.status_code, 201, child.text)
        self.assertEqual(child.json()['parent_study_id'], parent['id'])
        self.assertEqual(self.client.get('/api/author-studies/' + child.json()['id'] + '/reviews').json(), {'items': []})
        self.assertEqual(self.client.get('/api/author-studies').json()['total'], 2)

    def test_http_strict_input_and_result_digest_boundaries(self):
        for change in (lambda b: b.update(extra=True), lambda b: b['scan'].update(schema_version=True),
                       lambda b: b['scan']['months'][0]['patterns'].__setitem__(0, True),
                       lambda b: b.update(author_panel_ids=['invalid']), lambda b: b.update(title=' ')):
            body = deepcopy(self.body); change(body)
            response = self.client.post('/api/author-studies', json=body)
            self.assertEqual(response.status_code, 422, response.text)
        study = self.client.post('/api/author-studies', json=self.body).json()
        response = self.client.post('/api/author-studies/' + study['id'] + '/reviews', json={
            'study_digest': '0' * 64, 'decision': 'data_insufficient', 'actor': 'automation', 'note': 'wrong digest', 'idempotency_key': 'stale'})
        self.assertEqual(response.status_code, 409)
        for suffix in ('?limit=101', '?offset=100001'):
            self.assertEqual(self.client.get('/api/author-studies' + suffix).status_code, 422)
        self.assertEqual(self.client.get('/api/author-studies/author_study_' + '0' * 64).status_code, 404)


class StudyMigrationTests(unittest.TestCase):
    def test_genuine_schema11_failure_rolls_back_and_additive_upgrade_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workbench.sqlite3'
            with closing(db.connect(path)) as connection:
                connection.executescript(db.SCHEMA); connection.execute("INSERT INTO settings VALUES ('schema_version','1')")
                for version in range(1, 11): getattr(db, f'_migrate_v{version}')(connection)
                connection.execute("UPDATE settings SET value='11' WHERE key='schema_version'")
                connection.execute("INSERT INTO papers VALUES ('old','Historical','hash','now','{  \"untouched\": true }','paper.pdf')")
                snapshot = '\n'.join(connection.iterdump())
            original = db._migrate_v11
            def fail(connection): original(connection); raise RuntimeError('Injected schema12 failure')
            with patch.object(db, '_migrate_v11', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'Injected'): db.initialize(path)
            with closing(db.connect(path)) as connection: self.assertEqual('\n'.join(connection.iterdump()), snapshot)
            db.initialize(path)
            with closing(db.connect(path)) as connection:
                self.assertEqual(connection.execute('SELECT document FROM papers').fetchone()[0], '{  "untouched": true }')
                self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM author_studies').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM author_study_reviews').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM author_study_receipts').fetchone()[0], 0)
                self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
