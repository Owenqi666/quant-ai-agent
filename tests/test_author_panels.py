"""Bounded imports, durable exact receipts, corruption refusal and schema 11."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from paper_alpha.author_archive_contract import ADAPTER_VERSION, source_metadata
from paper_alpha.author_panel import evaluate, render_report
from paper_alpha.server import db, author_panels
from paper_alpha.server.api import create_app
from paper_alpha.server.author_panels import AuthorPanels
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import digest, json_text


def panel_fixture():
    months = ['2019-12'] + [f'2020-{month:02d}' for month in range(1, 13)]
    return {'schema_version': 1, 'kind': 'author_perturbed_monthly_panel', 'adapter_version': ADAPTER_VERSION,
            'source': source_metadata('USData.mat'),
            'selection': {'target_month': '2020-12', 'row_offset': 0, 'row_count': 2},
            'months': months, 'source_observation_dates': [month + '-28' for month in months],
            'rows': [{'source_row': index, 'asset': f'USData.mat:row:{index}', 'country': None,
                      'returns': [0.01] * 13, 'return_states': ['value'] * 13,
                      'dgw': 0.2, 'dgw_state': 'value', 'market_cap': 1.0, 'market_cap_state': 'value'}
                     for index in (1, 2)]}


class AuthorPanelServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'workspace')
        self.service = AuthorPanels(self.store)

    def create(self, **changes):
        return self.service.create(**({'title': 'Observed source rows', 'note': 'Controlled test input; no source authentication',
                                      'panel': panel_fixture(), 'idempotency_key': 'import'} | changes))

    def test_replay_reopen_and_new_key_preserve_exact_immutable_record(self):
        first = self.create(title='  Source rows  ')
        self.service = AuthorPanels(Store(self.store.root))
        self.assertEqual(self.create(title='  Source rows  '), first)
        self.assertEqual(self.create(title='  Source rows  ', idempotency_key='second'), first)
        self.assertEqual(self.service.get(first['id']), first)
        self.assertEqual(self.service.list()['total'], 1)
        self.assertEqual(len(self.store._read('SELECT * FROM author_panel_receipts')), 2)
        self.assertEqual(first['verification_scope'], 'normalized_panel_and_diagnostics')
        self.assertFalse(first['raw_source_reverified'])
        self.assertTrue(first['reference']['passed'])
        self.assertEqual(self.service.markdown(first['id']), render_report(first['result']))
        first['panel']['rows'][0]['returns'][0] = 99
        self.assertNotEqual(self.service.get(first['id'])['panel'], first['panel'])

    def test_exact_key_conflict_including_whitespace_does_not_create(self):
        original = self.create()
        for change in ({'title': 'Another'}, {'note': 'Another'}, {'title': ' Observed source rows'}):
            with self.subTest(change=change), self.assertRaises(ServiceError) as caught:
                self.create(**change)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.list()['total'], 1)
        self.assertEqual(self.service.get(original['id']), original)

    def test_parallel_same_request_creates_single_record_and_receipt(self):
        barrier = threading.Barrier(4)
        def create(_):
            barrier.wait(timeout=10)
            return self.create()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(create, range(4)))
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(self.service.list()['total'], 1)
        self.assertEqual(len(self.store._read('SELECT * FROM author_panel_receipts')), 1)

    def test_computation_precedes_writer_transaction(self):
        original = author_panels._reference
        def check(*args):
            # A distinct connection can take the writer lock during calculation.
            with db.transaction(self.store.db_path) as connection:
                connection.execute("INSERT OR REPLACE INTO settings VALUES ('diagnostic-test','independent')")
            return original(*args)
        with patch.object(author_panels, '_reference', side_effect=check):
            self.create()

    def test_failed_receipt_write_rolls_back_business_record(self):
        with db.transaction(self.store.db_path) as connection:
            connection.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON author_panel_receipts BEGIN SELECT RAISE(ABORT, 'injected'); END")
        import sqlite3
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'injected'):
            self.create()
        self.assertEqual(self.service.list()['total'], 0)

    def test_corrupt_payload_blocks_get_list_report_and_replay(self):
        value = self.create()
        with db.transaction(self.store.db_path) as connection:
            row = connection.execute('SELECT * FROM author_panels WHERE id=?', (value['id'],)).fetchone()
            body = json.loads(row['payload']); body['result']['summary']['formation_ready'] = 999
            connection.execute('UPDATE author_panels SET payload=? WHERE id=?', (json_text(body), value['id']))
        for action in (lambda: self.service.get(value['id']), self.service.list,
                       lambda: self.service.markdown(value['id']), self.create):
            with self.subTest(action=action), self.assertRaises(ServiceError) as caught:
                action()
            self.assertEqual(caught.exception.status, 409)

    def test_rehashed_result_tamper_fails_independent_reference(self):
        value = self.create()
        with db.transaction(self.store.db_path) as connection:
            row = connection.execute('SELECT * FROM author_panels WHERE id=?', (value['id'],)).fetchone()
            body = json.loads(row['payload']); body['result']['rows'][0]['momentum'] = 100.0
            fingerprint = digest(body); identity = 'author_panel_' + fingerprint
            connection.execute('DELETE FROM author_panel_receipts')
            connection.execute('UPDATE author_panels SET payload=?,digest=?,id=?,metadata_digest=? WHERE id=?',
                               (json_text(body), fingerprint, identity,
                                author_panels._metadata_digest(identity, fingerprint, row['created_at']), value['id']))
        with self.assertRaises(ServiceError) as caught:
            self.service.get(identity)
        self.assertEqual(caught.exception.status, 409)

    def test_changed_valid_timestamp_blocks_read_list_and_replay_without_writes(self):
        value = self.create()
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE author_panels SET created_at='1990-01-01T00:00:00+00:00'")
            before = '\n'.join(connection.iterdump())
        for action in (lambda: self.service.get(value['id']), self.service.list, self.create,
                       lambda: self.create(idempotency_key='new-key')):
            with self.subTest(action=action), self.assertRaises(ServiceError) as caught:
                action()
            self.assertEqual(caught.exception.status, 409)
        with closing(db.connect(self.store.db_path)) as connection:
            self.assertEqual('\n'.join(connection.iterdump()), before)

    def test_receipt_binds_original_response_metadata_even_when_row_is_rehashed(self):
        value = self.create()
        changed = '1990-01-01T00:00:00+00:00'
        metadata_digest = author_panels._metadata_digest(value['id'], value['digest'], changed)
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE author_panels SET created_at=?,metadata_digest=?', (changed, metadata_digest))
            before = '\n'.join(connection.iterdump())
        with self.assertRaises(ServiceError) as caught:
            self.create()
        self.assertEqual(caught.exception.status, 409)
        with closing(db.connect(self.store.db_path)) as connection:
            self.assertEqual('\n'.join(connection.iterdump()), before)

    def test_bad_receipt_refuses_replay_and_preserves_record(self):
        value = self.create()
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE author_panel_receipts SET request_digest='changed'")
        with self.assertRaises(ServiceError) as caught:
            self.create()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.get(value['id']), value)

    def test_missing_receipt_target_is_not_recreated(self):
        self.create()
        with closing(db.connect(self.store.db_path)) as connection:
            connection.execute('PRAGMA foreign_keys=OFF')
            connection.execute('DELETE FROM author_panels')
        with self.assertRaises(ServiceError) as caught:
            self.create()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.store._read('SELECT * FROM author_panels')), 0)

    def test_invalid_input_and_paging_boundaries(self):
        invalid_panel = panel_fixture(); invalid_panel['schema_version'] = True
        for change in ({'title': ' '}, {'note': 'x' * 4001}, {'title': '\ud800'}, {'idempotency_key': ''},
                       {'idempotency_key': None}, {'idempotency_key': '\udfff'}, {'panel': invalid_panel}):
            with self.subTest(change=change), self.assertRaises(ServiceError) as caught:
                self.create(**change)
            self.assertEqual(caught.exception.status, 422)
        for kwargs in ({'limit': True}, {'limit': 101}, {'limit': 0}, {'offset': True}, {'offset': -1}, {'offset': 100001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ServiceError) as caught:
                self.service.list(**kwargs)
            self.assertEqual(caught.exception.status, 422)
        self.assertEqual(self.service.list()['total'], 0)
        self.create(); self.create(title='Second', idempotency_key='second')
        first = self.service.list(limit=1, offset=0); second = self.service.list(limit=1, offset=1)
        self.assertEqual(first['total'], 2); self.assertEqual(second['total'], 2)
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertNotIn('panel', first['items'][0]); self.assertNotIn('rows', first['items'][0])

    def test_backup_restore_keeps_import_and_receipt(self):
        value = self.create()
        create_backup(self.store.root, self.root / 'backup')
        restore_backup(self.root / 'backup', self.root / 'restored')
        restored = AuthorPanels(Store(self.root / 'restored'))
        self.assertEqual(restored.get(value['id']), value)
        self.assertEqual(restored.create(title=value['title'], note=value['note'], panel=panel_fixture(), idempotency_key='import'), value)


class AuthorPanelHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / 'workspace')
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.body = {'title': 'Author row inspection', 'note': 'Fixture only', 'panel': panel_fixture(), 'idempotency_key': 'http-import'}

    def test_http_import_lightweight_list_detail_report_and_replay(self):
        response = self.client.post('/api/author-panels', json=self.body)
        self.assertEqual(response.status_code, 201, response.text)
        value = response.json(); path = '/api/author-panels/' + value['id']
        self.assertEqual(self.client.post('/api/author-panels', json=self.body).json(), value)
        self.assertEqual(self.client.get(path).json(), value)
        page = self.client.get('/api/author-panels').json()
        self.assertEqual(page['items'][0]['summary'], value['result']['summary'])
        self.assertNotIn('panel', page['items'][0])
        report = self.client.get(path + '/markdown')
        self.assertEqual(report.status_code, 200); self.assertTrue(report.headers['content-type'].startswith('text/markdown'))
        self.assertEqual(report.text, render_report(value['result']))
        self.assertIn('attachment', report.headers['content-disposition'])

    def test_http_strict_semantics_size_and_source_boundaries(self):
        for change in (lambda body: body.update(extra=True),
                       lambda body: body['panel'].update(schema_version=True),
                       lambda body: body['panel']['rows'][0].update(dgw=True),
                       lambda body: body['panel']['source'].update(sha256='0' * 64),
                       lambda body: body.update(idempotency_key=' ')):
            body = deepcopy(self.body); change(body)
            response = self.client.post('/api/author-panels', json=body)
            self.assertEqual(response.status_code, 422, response.text)
        response = self.client.post('/api/author-panels', content=' ' * (1024 * 1024 + 1), headers={'content-type': 'application/json'})
        self.assertEqual(response.status_code, 413)
        for suffix in ('?limit=101', '?offset=100001'):
            self.assertEqual(self.client.get('/api/author-panels' + suffix).status_code, 422)
        self.assertEqual(self.client.get('/api/author-panels/invalid').status_code, 422)
        self.assertEqual(self.client.get('/api/author-panels/author_panel_' + '0' * 64).status_code, 404)
        self.assertEqual(self.client.get('/api/author-panels').json()['total'], 0)


class AuthorPanelMigrationTests(unittest.TestCase):
    def test_genuine_v10_upgrade_rolls_back_on_failure_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workbench.sqlite3'
            with closing(db.connect(path)) as connection:
                connection.executescript(db.SCHEMA)
                connection.execute("INSERT INTO settings VALUES ('schema_version','1')")
                for version in range(1, 10):
                    getattr(db, f'_migrate_v{version}')(connection)
                connection.execute("UPDATE settings SET value='10' WHERE key='schema_version'")
                connection.execute("INSERT INTO papers VALUES ('paper','Historical','hash','now','{  \"raw\": true }','paper.pdf')")
                snapshot = '\n'.join(connection.iterdump())
            original = db._migrate_v10
            def fail(connection):
                original(connection)
                raise RuntimeError('Injected schema11 failure')
            with patch.object(db, '_migrate_v10', side_effect=fail), self.assertRaisesRegex(RuntimeError, 'Injected'):
                db.initialize(path)
            with closing(db.connect(path)) as connection:
                self.assertEqual('\n'.join(connection.iterdump()), snapshot)
            db.initialize(path)
            with closing(db.connect(path)) as connection:
                self.assertEqual(connection.execute('SELECT document FROM papers').fetchone()[0], '{  "raw": true }')
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM author_panels').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM author_panel_receipts').fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
                self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
