"""Actual loader + Store protection across encoding and frozen row subsets."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import csv
import hashlib
import io
from pathlib import Path
import re
import tempfile
import unittest
import uuid

from paper_alpha.server import observation_identity as observations
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import connect, transaction
from paper_alpha.server.research_guard import ResearchGuard
from paper_alpha.server.service import REPO, ServiceError, Store
from paper_alpha.storage import atomic_json, digest, json_text, read_json


def install_if_needed(store):
    """Development helper while root owns installation of the additive schema."""
    with transaction(store.db_path) as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_identities'").fetchone()
        if not exists:
            for statement in observations.SCHEMA.split(';'):
                if statement.strip():
                    connection.execute(statement)
            observations.seed_history(connection)


def downgrade_to_schema14(store):
    """Drop only additive v19 resources, leaving original business rows intact."""
    from paper_alpha.server.domain_research_jobs import SCHEMA as DOMAIN_SCHEMA
    from paper_alpha.server.semantic_annotations import SCHEMA as SEMANTIC_SCHEMA
    from tests.v020_schema_helpers import SCHEMA as V020_SCHEMA
    tables = re.findall(r'CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)', observations.SCHEMA + DOMAIN_SCHEMA + SEMANTIC_SCHEMA + V020_SCHEMA)
    with transaction(store.db_path) as connection:
        for table in reversed(tables):
            connection.execute(f'DROP TABLE IF EXISTS {table}')
        connection.execute("UPDATE settings SET value='14' WHERE key='schema_version'")
        connection.execute('DELETE FROM schema_migrations WHERE version>=15')


def register_variant(store, *, mode='encoded', dates=None, assets=None):
    """A new immutable input fixture; never modify the seeded dataset itself."""
    source = store._fetch('datasets', store.example_dataset_id)
    metadata = read_json(source['metadata_path'])
    with Path(source['data_path']).open(newline='') as stream:
        reader = csv.DictReader(stream)
        columns, rows = list(reader.fieldnames), list(reader)
    if dates is not None:
        rows = [row for row in rows if row['date'] in dates]
        metadata['calendar_dates'] = [day for day in metadata['calendar_dates'] if day in dates]
    if assets is not None:
        rows = [row for row in rows if row['asset'] in assets]
        metadata['universe'] = [asset for asset in metadata['universe'] if asset in assets]
    if mode == 'encoded':
        rows.sort(key=lambda row: (row['date'], tuple(-ord(char) for char in row['asset'])))
        for row in rows:
            row['volume'] = format(int(float(row['volume'])), '.8e')
        metadata['universe'] = list(reversed(metadata['universe']))
    if mode == 'append':
        last = metadata['calendar_dates'][-1]
        rows.extend([{**row, 'date': '2023-07-17'} for row in rows if row['date'] == last])
        metadata['calendar_dates'].append('2023-07-17')
    if mode == 'changed_values':
        for row in rows:
            for name in ('open', 'high', 'low', 'close', 'volume'):
                row[name] = str(float(row[name]) * 2)
    if mode == 'renamed_assets':
        for row in rows:
            row['asset'] = 'RENAMED_' + row['asset']
        metadata['universe'] = ['RENAMED_' + asset for asset in metadata['universe']]
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=columns, quoting=csv.QUOTE_ALL, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(rows)
    payload = buffer.getvalue().encode()
    metadata.update(version='new-title-never-erases-observation-history',
                    market_sha256=hashlib.sha256(payload).hexdigest(),
                    sessions=len(metadata['calendar_dates']), rows=len(rows))
    identity = str(uuid.uuid4())
    folder = store.root / 'datasets' / identity
    folder.mkdir()
    (folder / 'market.csv').write_bytes(payload)
    atomic_json(folder / 'metadata.json', metadata)
    with transaction(store.db_path) as connection:
        connection.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?)',
            (identity, 'Equivalent observation fixture', digest({'data': metadata['market_sha256'], 'metadata': metadata}),
             json_text(metadata), str(folder / 'market.csv'), str(folder / 'metadata.json')))
    return identity


class ObservationIdentityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'home')
        install_if_needed(self.store)
        self.paper = self.store.add_paper((REPO / 'examples/alpha101/paper.pdf').read_bytes(), 'Alpha101')
        raw = read_json(REPO / 'examples/alpha101/task.json')
        self.task = {key: value for key, value in raw.items() if key in {'evidence', 'hypotheses', 'candidates', 'evaluation', 'budget'}}

    def tearDown(self):
        self.temp.cleanup()

    def create(self, dataset=None, task=None, key=None):
        return self.store.create_research('Row reuse fixture', self.paper['id'], dataset or self.store.example_dataset_id,
                                          task or deepcopy(self.task), key)

    def unsafe(self):
        task = deepcopy(self.task)
        task['evaluation']['splits']['validation'] = {'start': '2023-04-03', 'end': '2023-06-30'}
        task['evaluation']['splits']['test'] = {'start': '2023-07-03', 'end': '2023-07-14'}
        return task

    def identity(self, dataset):
        with closing(connect(self.store.db_path)) as connection:
            return dict(connection.execute('SELECT * FROM observation_identities WHERE dataset_id=?', (dataset,)).fetchone())

    def blocked(self, dataset, task=None):
        with self.assertRaises(ServiceError) as raised:
            self.create(dataset, task or self.unsafe())
        self.assertEqual(raised.exception.status, 409)
        self.assertIn('reserved test interval', str(raised.exception))

    def test_encoding_order_and_numeric_equivalence_use_actual_engine_values(self):
        first = self.create()
        encoded = register_variant(self.store)
        second = self.create(encoded)
        old, new = self.identity(self.store.example_dataset_id), self.identity(encoded)
        self.assertNotEqual(old['market_sha256'], new['market_sha256'])
        self.assertEqual(old['panel_sha256'], new['panel_sha256'])
        self.assertEqual(old['index_digest'], new['index_digest'])
        self.assertEqual(old['row_count'], new['row_count'])
        self.blocked(encoded)
        status = ResearchGuard(self.store).get(second['id'])
        self.assertEqual(status['total_boundaries'], 2)
        self.assertEqual({row['research_id'] for row in status['boundaries']}, {first['id'], second['id']})

    def test_readonly_preflight_for_new_encoded_data_cannot_erase_reservation(self):
        self.create()
        encoded = register_variant(self.store)
        preflight = ResearchGuard(self.store).check(encoded, self.unsafe())
        self.assertFalse(preflight['allowed'])
        self.assertIn('exact reused row', preflight['reason'])
        with closing(connect(self.store.db_path)) as connection:
            self.assertIsNone(connection.execute('SELECT 1 FROM observation_identities WHERE dataset_id=?', (encoded,)).fetchone())
        self.assertTrue(ResearchGuard(self.store).check(encoded, self.task)['allowed'])

    def test_cropped_calendar_retains_shared_reserved_observations(self):
        self.create()
        metadata = read_json(self.store._fetch('datasets', self.store.example_dataset_id)['metadata_path'])
        dates = metadata['calendar_dates'][5:]
        cropped = register_variant(self.store, mode='plain', dates=set(dates))
        task = self.unsafe()
        task['evaluation']['splits']['train']['start'] = dates[0]
        self.blocked(cropped, task)

    def test_appended_data_and_asset_subset_retain_reserved_observations(self):
        self.create()
        for dataset in (register_variant(self.store, mode='append'),
                        register_variant(self.store, mode='plain', assets={'SYN001', 'SYN002', 'SYN003', 'SYN004', 'SYN005', 'SYN006'})):
            with self.subTest(dataset=dataset):
                self.blocked(dataset)

    def test_reverse_test_contamination_cannot_be_hidden_by_encoding(self):
        self.create()
        encoded = register_variant(self.store)
        task = deepcopy(self.task)
        task['evaluation']['splits'] = {
            'train': {'start': '2022-01-03', 'end': '2022-04-29'},
            'validation': {'start': '2022-05-02', 'end': '2022-06-30'},
            'test': {'start': '2022-07-01', 'end': '2022-09-30'}}
        with self.assertRaises(ServiceError) as raised:
            self.create(encoded, task)
        self.assertIn('previously declared development history', str(raised.exception))

    def test_shared_rows_outside_conflicting_dates_do_not_make_a_graph_union(self):
        self.create()
        # Reuse the old development history, but replace every old held-out row.
        variant = register_variant(self.store, mode='plain')
        dataset = self.store._fetch('datasets', variant)
        with Path(dataset['data_path']).open(newline='') as stream:
            reader = csv.DictReader(stream)
            columns, rows = list(reader.fieldnames), list(reader)
        for row in rows:
            if row['date'] >= '2023-04-03':
                for field in ('open', 'high', 'low', 'close', 'volume'):
                    row[field] = str(float(row[field]) * 2)
        buffer = io.StringIO(newline='')
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        payload = buffer.getvalue().encode()
        metadata = read_json(dataset['metadata_path'])
        metadata['market_sha256'] = hashlib.sha256(payload).hexdigest()
        Path(dataset['data_path']).write_bytes(payload)
        atomic_json(dataset['metadata_path'], metadata)
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE datasets SET metadata=?,sha256=? WHERE id=?',
                (json_text(metadata), digest({'data': metadata['market_sha256'], 'metadata': metadata}), variant))
        self.assertTrue(ResearchGuard(self.store).check(variant, self.unsafe())['allowed'])
        research = self.create(variant, self.unsafe())
        self.assertEqual(ResearchGuard(self.store).get(research['id'])['status'], 'protected')

    def test_changed_values_or_renamed_assets_are_an_explicit_coverage_limit(self):
        self.create()
        for mode in ('changed_values', 'renamed_assets'):
            with self.subTest(mode=mode):
                variant = register_variant(self.store, mode=mode)
                self.assertTrue(ResearchGuard(self.store).check(variant, self.unsafe())['allowed'])
                research = self.create(variant, self.unsafe())
                text = ' '.join(ResearchGuard(self.store).get(research['id'])['limitations'])
                self.assertIn('Asset renaming', text)
                self.assertIn('does not authenticate market sources', text)

    def test_missing_or_edited_old_row_index_blocks_new_encoding(self):
        self.create()
        encoded = register_variant(self.store)
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM observation_rows WHERE dataset_id=? AND date=(SELECT MIN(date) FROM observation_rows WHERE dataset_id=?)',
                               (self.store.example_dataset_id, self.store.example_dataset_id))
        check = ResearchGuard(self.store).check(encoded, self.task)
        self.assertFalse(check['allowed'])
        self.assertIn('missing or changed', check['reason'])
        with self.assertRaises(ServiceError):
            self.create(encoded)

    def test_index_and_self_digest_edit_are_caught_by_independent_revision_binding(self):
        self.create()
        encoded = register_variant(self.store)
        with transaction(self.store.db_path) as connection:
            identity = dict(connection.execute('SELECT * FROM observation_identities WHERE dataset_id=?', (self.store.example_dataset_id,)).fetchone())
            connection.execute("UPDATE observation_rows SET row_sha256=? WHERE dataset_id=?", ('0' * 64, self.store.example_dataset_id))
            panel, index = observations._hashers()
            for row in connection.execute('SELECT date,asset,row_sha256 FROM observation_rows WHERE dataset_id=? ORDER BY date,asset', (self.store.example_dataset_id,)):
                observations._update(panel, index, row)
            identity.update(panel_sha256=panel.hexdigest(), index_digest=index.hexdigest())
            identity['identity_digest'] = observations._record_digest(identity, 'identity_digest')
            connection.execute('UPDATE observation_identities SET panel_sha256=?,index_digest=?,identity_digest=? WHERE dataset_id=?',
                (identity['panel_sha256'], identity['index_digest'], identity['identity_digest'], self.store.example_dataset_id))
        check = ResearchGuard(self.store).check(encoded, self.task)
        self.assertFalse(check['allowed'])
        self.assertIn('revision binding', check['reason'])

    def test_unused_legacy_index_self_digest_cannot_grant_first_use_authorization(self):
        self.create()
        variant = register_variant(self.store)
        downgrade_to_schema14(self.store)
        self.store = Store(self.store.root)
        with transaction(self.store.db_path) as connection:
            identity = dict(connection.execute('SELECT * FROM observation_identities WHERE dataset_id=?', (variant,)).fetchone())
            connection.execute('UPDATE observation_rows SET row_sha256=? WHERE dataset_id=?', ('1' * 64, variant))
            panel, index = observations._hashers()
            for row in connection.execute('SELECT date,asset,row_sha256 FROM observation_rows WHERE dataset_id=? ORDER BY date,asset', (variant,)):
                observations._update(panel, index, row)
            identity.update(panel_sha256=panel.hexdigest(), index_digest=index.hexdigest())
            identity['identity_digest'] = observations._record_digest(identity, 'identity_digest')
            connection.execute('UPDATE observation_identities SET panel_sha256=?,index_digest=?,identity_digest=? WHERE dataset_id=?',
                (identity['panel_sha256'], identity['index_digest'], identity['identity_digest'], variant))
        check = ResearchGuard(self.store).check(variant, self.unsafe())
        self.assertFalse(check['allowed'])
        self.assertIn('actual engine input', check['reason'])

    def test_seed_helper_is_additive_and_does_not_repair_deleted_bindings(self):
        first = self.create()
        variant = register_variant(self.store)
        with transaction(self.store.db_path) as connection:
            result = observations.seed_history(connection)
            self.assertEqual(result['seeded_datasets'], 1)
            snapshot = [dict(row) for row in connection.execute('SELECT * FROM observation_identities ORDER BY dataset_id')]
            repeated = observations.seed_history(connection)
            self.assertEqual(repeated['seeded_datasets'], 0)
            self.assertEqual(snapshot, [dict(row) for row in connection.execute('SELECT * FROM observation_identities ORDER BY dataset_id')])
            connection.execute('DELETE FROM observation_revision_bindings WHERE revision_id=?', (first['latest_revision_id'],))
            observations.seed_history(connection)
            self.assertIsNone(connection.execute('SELECT 1 FROM observation_revision_bindings WHERE revision_id=?', (first['latest_revision_id'],)).fetchone())
        self.assertFalse(ResearchGuard(self.store).check(variant, self.task)['allowed'])

    def test_missing_old_binding_and_raw_guard_authority_are_refused(self):
        first = self.create()
        encoded = register_variant(self.store)
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM observation_revision_bindings WHERE revision_id=?', (first['latest_revision_id'],))
        self.assertFalse(ResearchGuard(self.store).check(encoded, self.task)['allowed'])
        with self.assertRaises(ServiceError):
            self.store.submit_run(first['latest_revision_id'], 'fixed', 'deleted-binding')

    def test_deleted_raw_boundary_still_blocks_a_new_encoding(self):
        first = self.create()
        encoded = register_variant(self.store)
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM research_guard_boundaries WHERE revision_id=?', (first['latest_revision_id'],))
        check = ResearchGuard(self.store).check(encoded, self.task)
        self.assertFalse(check['allowed'])
        self.assertIn('Historical temporal registry', check['reason'])

    def test_deleted_whole_historical_identity_is_never_regenerated(self):
        first = self.create()
        encoded = register_variant(self.store)
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM observation_revision_bindings WHERE dataset_id=?', (self.store.example_dataset_id,))
            connection.execute('DELETE FROM observation_rows WHERE dataset_id=?', (self.store.example_dataset_id,))
            connection.execute('DELETE FROM observation_identities WHERE dataset_id=?', (self.store.example_dataset_id,))
        with self.assertRaises(ServiceError):
            self.store.create_revision(first['id'], first['latest_revision_id'], self.task, 'No implicit repair', 'no-repair')
        self.assertFalse(ResearchGuard(self.store).check(encoded, self.task)['allowed'])
        with closing(connect(self.store.db_path)) as connection:
            self.assertIsNone(connection.execute('SELECT 1 FROM observation_identities WHERE dataset_id=?', (self.store.example_dataset_id,)).fetchone())

    def test_schema14_migration_preserves_old_rows_and_adds_frozen_indexes(self):
        first = self.create()
        variant = register_variant(self.store)
        second = self.create(variant)
        downgrade_to_schema14(self.store)
        with closing(connect(self.store.db_path)) as connection:
            tables = ('datasets', 'researches', 'revisions', 'research_guard_data', 'research_guard_boundaries')
            old = {table: [dict(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}
        files = {path.relative_to(self.store.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.store.root.rglob('*') if path.is_file() and path.name not in {'workbench.sqlite3', 'workbench.sqlite3-shm', 'workbench.sqlite3-wal', 'workbench.sqlite3.schema.lock'}}
        migrated = Store(self.store.root)
        with closing(connect(migrated.db_path)) as connection:
            after = {table: [dict(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}
            from paper_alpha.server.db import SCHEMA_VERSION
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM observation_revision_bindings').fetchone()[0], 2)
        self.assertEqual(old, after)
        for name, checksum in files.items():
            self.assertEqual(hashlib.sha256((migrated.root / name).read_bytes()).hexdigest(), checksum)
        self.assertEqual(ResearchGuard(migrated).get(first['id'])['total_boundaries'], 2)
        with self.assertRaises(ServiceError):
            migrated.create_revision(second['id'], second['latest_revision_id'], self.unsafe(), 'Re-encoded unsafe', 'migration-unsafe')

    def test_related_old_conflict_needs_a_shared_current_row_at_conflict_date(self):
        early = deepcopy(self.task)
        early['evaluation']['splits'] = {
            'train': {'start': '2022-01-03', 'end': '2022-04-29'},
            'validation': {'start': '2022-05-02', 'end': '2022-06-30'},
            'test': {'start': '2022-07-01', 'end': '2022-09-30'}}
        self.create(task=early)
        encoded = register_variant(self.store)
        old_b = self.create(encoded, early)
        downgrade_to_schema14(self.store)
        # A deliberately conflicted schema14 fixture. No production mutation
        # permits this; migration must retain its old declarations truthfully.
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE revisions SET task=?,digest=? WHERE id=?',
                (json_text(self.task), digest(self.task), old_b['latest_revision_id']))
            connection.execute('UPDATE research_guard_boundaries SET task_digest=?,development_end=?,test_start=?,test_end=? WHERE revision_id=?',
                (digest(self.task), '2023-03-31', '2023-04-03', '2023-07-14', old_b['latest_revision_id']))
        self.store = Store(self.store.root)
        self.assertEqual(ResearchGuard(self.store).get(old_b['id'])['status'], 'historical_conflict')
        candidate = register_variant(self.store, mode='changed_values')
        dataset = self.store._fetch('datasets', candidate)
        with Path(dataset['data_path']).open(newline='') as stream:
            reader = csv.DictReader(stream)
            columns, rows = list(reader.fieldnames), list(reader)
        for row in rows:
            if row['date'] == '2022-01-03':
                for field in ('open', 'high', 'low', 'close', 'volume'):
                    row[field] = str(float(row[field]) / 2)
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader(); writer.writerows(rows)
        payload = stream.getvalue().encode()
        metadata = read_json(dataset['metadata_path'])
        metadata['market_sha256'] = hashlib.sha256(payload).hexdigest()
        Path(dataset['data_path']).write_bytes(payload)
        atomic_json(dataset['metadata_path'], metadata)
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE datasets SET metadata=?,sha256=? WHERE id=?',
                (json_text(metadata), digest({'data': metadata['market_sha256'], 'metadata': metadata}), candidate))
        self.assertTrue(ResearchGuard(self.store).check(candidate, self.unsafe())['allowed'])
        current = self.create(candidate, self.unsafe())
        status = ResearchGuard(self.store).get(current['id'])
        self.assertEqual(status['total_boundaries'], 3)
        self.assertEqual(status['status'], 'protected')
        self.assertEqual(status['total_conflicts'], 0)

    def test_invalid_legacy_input_is_retained_as_unresolved_not_migration_failure(self):
        self.create()
        source = self.store._fetch('datasets', self.store.example_dataset_id)
        downgrade_to_schema14(self.store)
        original = Path(source['data_path']).read_bytes() + b'\n'
        Path(source['data_path']).write_bytes(original)
        migrated = Store(self.store.root)
        self.assertEqual(Path(source['data_path']).read_bytes(), original)
        with closing(connect(migrated.db_path)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM observation_unresolved').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM revisions').fetchone()[0], 1)
        variant = register_variant(migrated)
        check = ResearchGuard(migrated).check(variant, self.task)
        self.assertFalse(check['allowed'])
        self.assertIn('unresolved', check['reason'])

    def test_concurrent_unsafe_and_safe_reencoded_creation_is_atomic_and_idempotent(self):
        encoded = register_variant(self.store)
        early = deepcopy(self.task)
        early['evaluation']['splits'] = {
            'train': {'start': '2022-01-03', 'end': '2022-04-29'},
            'validation': {'start': '2022-05-02', 'end': '2022-06-30'},
            'test': {'start': '2022-07-01', 'end': '2022-09-30'}}
        def action(item):
            dataset, task, key = item
            try:
                return self.create(dataset, task, key)
            except ServiceError as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(action, [(None, self.task, 'late'), (encoded, early, 'early')]))
        self.assertEqual(sum(isinstance(value, dict) for value in values), 1)
        self.assertEqual(sum(isinstance(value, ServiceError) for value in values), 1)
        winner = next(value for value in values if isinstance(value, dict))
        dataset = winner['dataset_id']
        task, key = (self.task, 'late') if dataset == self.store.example_dataset_id else (early, 'early')
        self.assertEqual(self.create(dataset, task, key), winner)

    def test_backup_restores_index_and_reuse_refusal(self):
        self.create()
        variant = register_variant(self.store)
        self.create(variant)
        create_backup(self.store.root, self.root / 'backup')
        restore_backup(self.root / 'backup', self.root / 'restored')
        restored = Store(self.root / 'restored')
        with self.assertRaises(ServiceError) as raised:
            restored.create_research('Restored reuse', self.paper['id'], variant, self.unsafe())
        self.assertIn('reserved test interval', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
