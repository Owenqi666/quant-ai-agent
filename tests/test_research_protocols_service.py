"""Immutable rule storage against isolated SQLite workspaces, without market data."""
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

from paper_alpha import research_protocol
from paper_alpha.server import db, research_protocols
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.service import ServiceError, Store
from paper_alpha.storage import digest, json_text


class ResearchProtocolServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.protocols = ResearchProtocols(self.store)
        self.presets = {item['config']['mode']: item for item in research_protocol.presets()}
        self.config = deepcopy(self.presets['project']['config'])

    def save(self, **changes):
        args = {'title': 'Window and missing-data rules', 'note': 'Controlled fixture only.', 'config': self.config}
        return self.protocols.create(**{**args, **changes})

    def raw_row(self, identity):
        with closing(db.connect(self.store.db_path)) as connection:
            return dict(connection.execute('SELECT * FROM research_protocols WHERE id=?', (identity,)).fetchone())

    def replace_payload(self, identity, change, *, rehash=False):
        row = self.raw_row(identity)
        body = json.loads(row['payload'])
        change(body)
        replacement = 'protocol_' + digest(body) if rehash else identity
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_protocols SET payload=?,id=?,digest=? WHERE id=?',
                               (json_text(body), replacement, digest(body) if rehash else row['digest'], identity))
        return replacement

    def test_canonical_body_replay_survives_response_loss_and_service_reopen(self):
        first = self.save(title='  Window and missing-data rules  ')
        with patch.object(research_protocols, 'now', return_value='2099-01-01T00:00:00+00:00'):
            replay = ResearchProtocols(Store(self.store.root)).create(
                title='Window and missing-data rules', note='Controlled fixture only.',
                config=dict(reversed(list(self.config.items()))))
        self.assertEqual(first, replay)
        self.assertEqual(self.protocols.get(first['id']), first)
        self.assertEqual(self.protocols.list()['total'], 1)
        self.assertEqual(first['id'], 'protocol_' + first['digest'])
        body = {key: first[key] for key in research_protocols.BODY_FIELDS}
        self.assertEqual(digest(body), first['digest'])
        self.assertEqual(research_protocol.config_digest(first['config']), first['config_digest'])
        self.assertEqual(first['changes'], [])

    def test_preview_and_saved_protocol_share_the_exact_config_digest(self):
        for config in (self.config, {**self.config, 'mom_window_months': 6}, self.presets['paper']['config']):
            with self.subTest(config=config):
                saved = self.save(config=config)
                preview = research_protocol.preview(config, '2024-03')
                self.assertEqual(saved['config'], preview['config'])
                self.assertEqual(saved['config_digest'], preview['config_digest'])
                self.assertEqual(saved['semantics_version'], preview['semantics_version'])

    def test_same_body_concurrent_requests_create_exactly_one_record(self):
        gate = threading.Barrier(4)
        def save(_):
            gate.wait(timeout=10)
            return self.save()
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(save, range(4)))
        self.assertTrue(all(value == values[0] for value in values))
        self.assertEqual(self.protocols.list()['total'], 1)

    def test_parent_diff_keeps_paper_and_project_versions_distinct(self):
        paper = self.save(title='Paper definition, unresolved', config=self.presets['paper']['config'])
        before = self.raw_row(paper['id'])
        derived = self.save(parent_id=paper['id'])
        changes = {item['field']: item for item in derived['changes']}
        self.assertEqual(changes['mode'], {'field': 'mode', 'before': 'paper', 'after': 'project'})
        self.assertEqual(changes['missing_policy']['before'], 'unresolved')
        self.assertEqual(changes['missing_policy']['after'], 'complete')
        self.assertIn('id_window_months', changes)
        self.assertEqual(before, self.raw_row(paper['id']))
        self.assertEqual(self.protocols.get(paper['id']), paper)
        modified = self.save(parent_id=derived['id'], config={**self.config, 'mom_window_months': 6})
        self.assertEqual(modified['changes'], [{'field': 'mom_window_months', 'before': 11, 'after': 6}])
        self.assertEqual(len({paper['id'], derived['id'], modified['id']}), 3)

    def test_root_variants_compare_to_mode_preset_and_preserve_declared_sources(self):
        variant = self.save(config={**self.config, 'mom_skip_months': 2})
        self.assertEqual(variant['changes'], [{'field': 'mom_skip_months', 'before': 1, 'after': 2}])
        self.assertEqual(variant['sources'], self.presets['project']['sources'])
        changed = deepcopy(list(self.presets.values()))
        for preset in changed:
            preset['sources'] = ['A later server evidence statement, not the saved snapshot.']
            preset['unresolved'] = ['A later unresolved statement.']
        with patch.object(research_protocol, 'presets', return_value=changed):
            self.assertEqual(self.protocols.get(variant['id']), variant)
        # Caller-owned dictionaries and response values are never retained.
        variant['config']['mom_skip_months'] = 10
        self.assertEqual(self.protocols.get(variant['id'])['config']['mom_skip_months'], 2)

    def test_invalid_parent_config_and_user_text_do_not_insert_records(self):
        cases = [
            ({'parent_id': 'not-a-protocol'}, 422),
            ({'parent_id': 'protocol_' + '0' * 64}, 404),
            ({'config': {**self.config, 'mode': 'paper'}}, 422),
            ({'config': {**self.config, 'sources': ['Invented evidence']}}, 422),
            ({'config': {**self.config, 'mom_window_months': True}}, 422),
            ({'config': {**self.config, 'min_coverage': float('nan')}}, 422),
            ({'title': '   '}, 422), ({'title': 'x' * 201}, 422),
            ({'note': 'x' * 4001}, 422), ({'note': None}, 422),
            ({'title': '\ud800'}, 422), ({'note': '\udfff'}, 422),
        ]
        for args, status in cases:
            with self.subTest(args=args), self.assertRaises(ServiceError) as caught:
                self.save(**args)
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(self.protocols.list()['total'], 0)

    def test_body_tamper_blocks_detail_listing_and_duplicate_replay(self):
        value = self.save()
        self.replace_payload(value['id'], lambda body: body.update(note='Changed behind the service'))
        for operation in (lambda: self.protocols.get(value['id']), self.protocols.list, self.save):
            with self.subTest(operation=operation), self.assertRaises(ServiceError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.store._read('SELECT id FROM research_protocols')), 1)

    def test_parent_tamper_blocks_child_read_and_new_derivation(self):
        parent = self.save()
        child = self.save(parent_id=parent['id'], config={**self.config, 'mom_window_months': 6})
        self.replace_payload(parent['id'], lambda body: body['sources'].append('Forged source'))
        for operation in (lambda: self.protocols.get(child['id']),
                          lambda: self.save(parent_id=child['id'], title='Further derivation')):
            with self.assertRaises(ServiceError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.store._read('SELECT id FROM research_protocols')), 2)

    def test_parent_and_child_are_read_from_one_sqlite_snapshot(self):
        parent = self.save()
        child = self.save(parent_id=parent['id'], title='Derived protocol')
        original = self.protocols._record
        changed = False
        def change_after_child_read(row):
            nonlocal changed
            value = original(row)
            if row['id'] == child['id'] and not changed:
                changed = True
                self.replace_payload(parent['id'], lambda body: body.update(note='Concurrent damage'))
            return value
        with patch.object(self.protocols, '_record', side_effect=change_after_child_read):
            self.assertEqual(self.protocols.get(child['id']), child)
        self.assertTrue(changed)
        with self.assertRaises(ServiceError) as caught:
            self.protocols.get(child['id'])
        self.assertEqual(caught.exception.status, 409)

    def test_consistently_rehashed_invalid_changes_or_semantics_are_rejected(self):
        for change in (lambda body: body['changes'].append({'field': 'mode', 'before': 'paper', 'after': 'project'}),
                       lambda body: body.update(semantics_version='unsupported-version'),
                       lambda body: body.update(config_digest='0' * 64)):
            with self.subTest(change=change):
                value = self.save(title=str(change))
                identity = self.replace_payload(value['id'], change, rehash=True)
                with self.assertRaises(ServiceError) as caught:
                    self.protocols.get(identity)
                self.assertEqual(caught.exception.status, 409)

    def test_foreign_key_protects_parents_and_missing_parent_fails_closed(self):
        parent = self.save()
        child = self.save(parent_id=parent['id'], title='Derived protocol')
        with self.assertRaises(sqlite3.IntegrityError), db.transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM research_protocols WHERE id=?', (parent['id'],))
        # Simulate corruption through an external connection without FK enforcement.
        with closing(sqlite3.connect(self.store.db_path)) as connection:
            connection.execute('DELETE FROM research_protocols WHERE id=?', (parent['id'],))
            connection.commit()
        with self.assertRaises(ServiceError) as caught:
            self.protocols.get(child['id'])
        self.assertEqual(caught.exception.status, 409)

    def test_listing_is_bounded_stable_and_reports_total_without_truncating_rows(self):
        values = []
        with patch.object(research_protocols, 'now', return_value='2026-10-02T00:00:00+00:00'):
            for number in range(5):
                values.append(self.save(title=f'Rule {number}'))
        expected = sorted(values, key=lambda value: value['id'], reverse=True)
        self.assertEqual(self.protocols.list(limit=2, offset=1),
                         {'items': expected[1:3], 'total': 5, 'limit': 2, 'offset': 1})
        self.assertEqual(self.protocols.list(limit=2, offset=5)['items'], [])
        for args in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': 1.0},
                     {'offset': -1}, {'offset': 100001}, {'offset': False}, {'offset': '1'}):
            with self.subTest(args=args), self.assertRaises(ServiceError) as caught:
                self.protocols.list(**args)
            self.assertEqual(caught.exception.status, 422)

    def test_chain_limit_is_explicit_and_old_chain_remains_readable(self):
        with patch.object(research_protocols, 'MAX_CHAIN_LENGTH', 3):
            parent = None
            for index in range(3):
                parent = self.save(title=f'Rule version {index}', parent_id=parent['id'] if parent else None)
            with self.assertRaises(ServiceError) as caught:
                self.save(title='Fourth version', parent_id=parent['id'])
            self.assertEqual(caught.exception.status, 413)
            self.assertEqual(self.protocols.get(parent['id']), parent)
            self.assertEqual(self.protocols.list()['total'], 3)

    def test_page_verification_budget_rejects_instead_of_returning_partial_page(self):
        for index in range(3):
            self.save(title=f'Independent rule {index}')
        with patch.object(research_protocols, 'MAX_VERIFIED_RECORDS', 2):
            with self.assertRaises(ServiceError) as caught:
                self.protocols.list(limit=3)
            self.assertEqual(caught.exception.status, 413)
            self.assertEqual(len(self.protocols.list(limit=2)['items']), 2)

    def test_saving_rules_does_not_modify_existing_workbench_records_or_files(self):
        self.store.seed_example()
        with closing(db.connect(self.store.db_path)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'research_protocols'")]
            before = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                      for table in tables}
        files_before = {str(path.relative_to(self.store.root)): path.read_bytes()
                        for path in self.store.root.rglob('*') if path.is_file()
                        and not path.name.startswith('workbench.sqlite3')}
        self.save()
        with closing(db.connect(self.store.db_path)) as connection:
            after = {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
                     for table in tables}
        files_after = {str(path.relative_to(self.store.root)): path.read_bytes()
                       for path in self.store.root.rglob('*') if path.is_file()
                       and not path.name.startswith('workbench.sqlite3')}
        self.assertEqual(before, after)
        self.assertEqual(files_before, files_after)


if __name__ == '__main__':
    unittest.main()
