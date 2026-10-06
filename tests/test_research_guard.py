"""Actual Store boundaries: reservations survive revisions, copies and restore."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import unittest

from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import connect, transaction
from paper_alpha.server.research_guard import ResearchGuard, SCHEMA, seed_history
from paper_alpha.server.service import REPO, ServiceError, Store
from paper_alpha.storage import atomic_json, digest, json_text, read_json


def legacy_schema(home):
    """Build an actual schema13 fixture, removing every additive v18 table.

    This does not weaken production's schema detection or migration checks.
    """
    from paper_alpha.server.research_jobs import SCHEMA as JOB_SCHEMA
    from paper_alpha.server.research_claims import SCHEMA as CLAIM_SCHEMA
    from paper_alpha.server.observation_identity import SCHEMA as OBSERVATION_SCHEMA
    from paper_alpha.server.domain_research_jobs import SCHEMA as DOMAIN_SCHEMA
    from paper_alpha.server.semantic_annotations import SCHEMA as SEMANTIC_SCHEMA
    from tests.v020_schema_helpers import SCHEMA as V020_SCHEMA
    tables = re.findall(r'CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)',
                        SCHEMA + JOB_SCHEMA + CLAIM_SCHEMA + OBSERVATION_SCHEMA + DOMAIN_SCHEMA + SEMANTIC_SCHEMA + V020_SCHEMA)
    with transaction(home / 'workbench.sqlite3') as connection:
        for table in reversed(tables):
            connection.execute(f'DROP TABLE IF EXISTS {table}')
        connection.execute("UPDATE settings SET value='13' WHERE key='schema_version'")
        connection.execute('DELETE FROM schema_migrations WHERE version>=14')


class ResearchGuardCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.store = Store(self.home)
        self.paper = self.store.add_paper((REPO / 'examples/alpha101/paper.pdf').read_bytes(), '101 Formulaic Alphas')
        raw = read_json(REPO / 'examples/alpha101/task.json')
        self.task = {key: value for key, value in raw.items() if key in {'evidence', 'hypotheses', 'candidates', 'evaluation', 'budget'}}

    def tearDown(self):
        self.temp.cleanup()

    def create(self, task=None, key=None, dataset_id=None):
        return self.store.create_research('Controlled Alpha101', self.paper['id'],
            dataset_id or self.store.example_dataset_id, task or deepcopy(self.task), key)

    def unsafe(self):
        task = deepcopy(self.task)
        task['evaluation']['splits']['validation'] = {'start': '2023-04-03', 'end': '2023-06-30'}
        task['evaluation']['splits']['test'] = {'start': '2023-07-03', 'end': '2023-07-14'}
        return task

    def early(self):
        task = deepcopy(self.task)
        task['evaluation']['splits'] = {
            'train': {'start': '2022-01-03', 'end': '2022-04-29'},
            'validation': {'start': '2022-05-02', 'end': '2022-06-30'},
            'test': {'start': '2022-07-01', 'end': '2022-09-30'}}
        return task

    def counts(self):
        with closing(connect(self.store.db_path)) as connection:
            return {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                    for table in ('researches', 'revisions', 'runs', 'research_guard_data', 'research_guard_boundaries', 'research_guard_events')}

    def assertBlocked(self, action, phrase):
        before = self.counts()
        with self.assertRaises(ServiceError) as raised:
            action()
        self.assertEqual(raised.exception.status, 409)
        self.assertIn(phrase, str(raised.exception))
        self.assertEqual(self.counts(), before)

    def test_revision_cannot_relabel_original_test_as_validation(self):
        research = self.create()
        self.assertBlocked(lambda: self.store.create_revision(research['id'], research['latest_revision_id'],
            self.unsafe(), 'Attempt test relabel', 'unsafe-revision'), 'reserved test interval')
        clean = self.store.create_revision(research['id'], research['latest_revision_id'], self.task,
            'Same data and temporal policy', 'safe-revision')
        self.assertEqual(clean['number'], 2)
        self.assertEqual(ResearchGuard(self.store).get(research['id'])['status'], 'protected')

    def test_copied_research_and_changed_paper_title_cannot_reset_guard(self):
        self.create()
        self.assertBlocked(lambda: self.create(self.unsafe(), 'copied-unsafe'), 'reserved test interval')
        copy = self.store.create_research('Completely different title', self.paper['id'],
            self.store.example_dataset_id, self.task, 'copied-safe')
        status = ResearchGuard(self.store).get(copy['id'])
        self.assertEqual(status['total_boundaries'], 2)
        self.assertEqual(len({r['research_id'] for r in status['boundaries']}), 2)

    def test_same_csv_reregistered_with_changed_metadata_version_is_same_content(self):
        original = self.create()
        source = self.store._fetch('datasets', self.store.example_dataset_id)
        metadata = read_json(source['metadata_path'])
        metadata['version'] = 'renamed-version-does-not-reset-history'
        folder = self.home / 'datasets' / 'renamed-dataset'
        folder.mkdir()
        (folder / 'market.csv').write_bytes(Path(source['data_path']).read_bytes())
        atomic_json(folder / 'metadata.json', metadata)
        with transaction(self.store.db_path) as connection:
            connection.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?)', ('renamed-dataset', 'Changed title',
                digest({'data': metadata['market_sha256'], 'metadata': metadata}), json_text(metadata),
                str(folder / 'market.csv'), str(folder / 'metadata.json')))
        self.assertBlocked(lambda: self.create(self.unsafe(), 'renamed-unsafe', 'renamed-dataset'), 'reserved test interval')
        safe = self.create(self.task, 'renamed-safe', 'renamed-dataset')
        self.assertEqual(ResearchGuard(self.store).get(original['id'])['data_sha256'],
                         ResearchGuard(self.store).get(safe['id'])['data_sha256'])

    def test_entire_computation_prehistory_is_protected(self):
        self.create(self.early())
        task = deepcopy(self.task)
        # Named train/validation are both later than the old held-out interval;
        # worker still computes their rolling history from calendar[0].
        task['evaluation']['splits']['train'] = {'start': '2022-10-03', 'end': '2022-12-30'}
        task['evaluation']['splits']['validation'] = {'start': '2023-01-02', 'end': '2023-03-31'}
        self.assertBlocked(lambda: self.create(task), 'reserved test interval')
        preflight = ResearchGuard(self.store).check(self.store.example_dataset_id, task)
        self.assertFalse(preflight['allowed'])
        self.assertEqual(preflight['development']['start'], '2022-01-03')

    def test_previously_declared_development_cannot_be_called_fresh_test(self):
        self.create()
        self.assertBlocked(lambda: self.create(self.early()), 'previously declared development history')

    def test_concurrent_conflicting_creations_have_only_one_atomic_winner(self):
        def create(task, key):
            try:
                return self.create(task, key)
            except ServiceError as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda item: create(*item), [(self.task, 'late'), (self.early(), 'early')]))
        self.assertEqual(sum(isinstance(r, dict) for r in responses), 1)
        self.assertEqual(sum(isinstance(r, ServiceError) and r.status == 409 for r in responses), 1)
        self.assertEqual(self.counts()['researches'], 1)
        self.assertEqual(self.counts()['research_guard_boundaries'], 1)

    def test_idempotent_concurrent_create_revision_submission_and_conflict(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(lambda _: self.create(self.task, 'create-key'), range(2)))
        self.assertEqual(created[0], created[1])
        research = created[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            revisions = list(pool.map(lambda _: self.store.create_revision(research['id'], research['latest_revision_id'],
                self.task, 'Same task', 'revision-key'), range(2)))
        self.assertEqual(revisions[0], revisions[1])
        revision = revisions[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(lambda _: self.store.submit_run(revision['id'], 'fixed', 'run-key'), range(2)))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(self.counts()['research_guard_boundaries'], 2)
        self.assertEqual(self.counts()['research_guard_events'], 1)
        self.assertBlocked(lambda: self.store.create_revision(research['id'], research['latest_revision_id'],
            self.unsafe(), 'Same task', 'revision-key'), 'already used')
        self.assertEqual(self.store.create_revision(research['id'], research['latest_revision_id'],
            self.task, 'Same task', 'revision-key'), revision)

    def test_guard_restores_with_backup_and_rejects_same_attack(self):
        research = self.create()
        before = ResearchGuard(self.store).get(research['id'])
        create_backup(self.home, self.root / 'backup')
        restore_backup(self.root / 'backup', self.root / 'restored')
        restored = Store(self.root / 'restored')
        self.assertEqual(ResearchGuard(restored).get(research['id']), before)
        with self.assertRaises(ServiceError) as raised:
            restored.create_research('After restore', self.paper['id'], restored.example_dataset_id, self.unsafe())
        self.assertIn('reserved test interval', str(raised.exception))

    def test_data_and_boundary_integrity_are_checked_before_execution(self):
        research = self.create()
        revision_id = research['latest_revision_id']
        with transaction(self.store.db_path) as connection:
            connection.execute("UPDATE research_guard_boundaries SET development_end='2022-12-30' WHERE revision_id=?", (revision_id,))
        self.assertBlocked(lambda: self.store.submit_run(revision_id, 'fixed', 'tampered-boundary'), 'does not match')
        # Read-only status does not falsely claim pristine data after file edits.
        dataset = self.store._fetch('datasets', self.store.example_dataset_id)
        with Path(dataset['data_path']).open('ab') as stream:
            stream.write(b'\n')
        status = ResearchGuard(self.store).get(research['id'])
        self.assertEqual(status['status'], 'unresolved')
        self.assertBlocked(lambda: self.store.submit_run(revision_id, 'fixed', 'tampered-data'), 'cannot verify')

    def test_legacy_unsafe_revisions_and_queued_runs_are_preserved_then_refused(self):
        research = self.create()
        legacy_schema(self.home)
        unsafe = self.unsafe()
        with transaction(self.store.db_path) as connection:
            connection.execute('INSERT INTO revisions VALUES (?,?,?,?,?,?,?)', ('legacy-unsafe', research['id'], 2,
                json_text(unsafe), 'Historical unsafe split', digest(unsafe), '2026-01-02T00:00:00+00:00'))
            connection.execute('UPDATE researches SET latest_revision_id=? WHERE id=?', ('legacy-unsafe', research['id']))
            connection.execute('INSERT INTO runs(id,revision_id,research_id,mode,status,created_at,idempotency_key,request_digest) VALUES (?,?,?,?,?,?,?,?)',
                ('legacy-queued', 'legacy-unsafe', research['id'], 'fixed', 'queued', '2026-01-03T00:00:00+00:00',
                 'legacy-run-key', digest({'revision_id': 'legacy-unsafe', 'mode': 'fixed'})))
            business_before = {t: [dict(r) for r in connection.execute(f'SELECT * FROM {t}')]
                for t in ('researches', 'revisions', 'runs', 'papers', 'datasets', 'events', 'mutation_receipts')}
        files_before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*')
                        if p.is_file() and p.suffix not in {'.sqlite3', '.lock'} and 'workbench.sqlite3' not in p.name}
        migrated = Store(self.home)
        with closing(connect(migrated.db_path)) as connection:
            business_after = {t: [dict(r) for r in connection.execute(f'SELECT * FROM {t}')]
                for t in business_before}
        self.assertEqual(business_before, business_after)
        self.assertEqual(files_before, {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*')
                        if p.is_file() and p.suffix not in {'.sqlite3', '.lock'} and 'workbench.sqlite3' not in p.name})
        status = ResearchGuard(migrated).get(research['id'])
        self.assertEqual(status['status'], 'historical_conflict')
        self.assertEqual({b['origin'] for b in status['boundaries']}, {'legacy'})
        self.assertGreater(status['total_conflicts'], 0)
        before = self.counts()
        with self.assertRaises(ServiceError):
            migrated.submit_run('legacy-unsafe', 'fixed', 'new-unsafe-run')
        self.assertEqual(self.counts(), before)
        # Existing acknowledgments can be recovered, but do not authorize a
        # newly executed unsafe legacy revision.
        self.assertEqual(migrated.submit_run('legacy-unsafe', 'fixed', 'legacy-run-key')['id'], 'legacy-queued')
        self.assertIsNone(migrated.claim('worker'))
        failed = migrated.get_run('legacy-queued')
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['attempt_count'], 0)
        self.assertEqual(failed['attempts'], [])
        self.assertIn('reserved test interval', failed['error'])
        self.assertEqual(migrated.events('legacy-queued')[-1]['kind'], 'temporal_guard_blocked')
        with self.assertRaises(ServiceError):
            migrated.retry_run('legacy-queued')

    def test_migration_seeding_is_additive_and_idempotent(self):
        research = self.create()
        with transaction(self.store.db_path) as connection:
            seeded = seed_history(connection)
        self.assertEqual(seeded, {'seeded': 0, 'unresolved_revision_ids': []})
        self.assertEqual(ResearchGuard(self.store).list()['items'][0]['research_id'], research['id'])
        for arguments in ({'limit': 0}, {'offset': -1}, {'limit': True}):
            with self.assertRaises(ServiceError):
                ResearchGuard(self.store).list(**arguments)

    def test_unresolved_legacy_task_blocks_new_research_on_same_csv(self):
        research = self.create()
        legacy_schema(self.home)
        # Preserve a historically corrupted digest as-is. Migration must not
        # repair its task or allow a renamed research to forget its unknown
        # temporal exposure.
        with transaction(self.store.db_path) as connection:
            connection.execute("UPDATE revisions SET digest=? WHERE id=?", ('0' * 64, research['latest_revision_id']))
            raw_before = dict(connection.execute('SELECT * FROM revisions WHERE id=?', (research['latest_revision_id'],)).fetchone())
        migrated = Store(self.home)
        status = ResearchGuard(migrated).get(research['id'])
        self.assertEqual(status['status'], 'unresolved')
        with closing(connect(migrated.db_path)) as connection:
            self.assertEqual(dict(connection.execute('SELECT * FROM revisions WHERE id=?', (research['latest_revision_id'],)).fetchone()), raw_before)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM research_guard_unresolved').fetchone()[0], 1)
        self.assertFalse(ResearchGuard(migrated).check(migrated.example_dataset_id, self.task)['allowed'])
        with self.assertRaises(ServiceError) as raised:
            migrated.create_research('New title cannot reset unknown history', self.paper['id'],
                migrated.example_dataset_id, self.task)
        self.assertIn('Historical temporal boundary is unresolved', str(raised.exception))
        with transaction(migrated.db_path) as connection:
            again = seed_history(connection)
        self.assertEqual(again, {'seeded': 0, 'unresolved_revision_ids': [research['latest_revision_id']]})

    def test_deleted_older_boundary_cannot_reopen_reserved_test_in_copied_research(self):
        research = self.create(key='original-key')
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM research_guard_boundaries WHERE revision_id=?',
                               (research['latest_revision_id'],))
        # The source revision survives deletion and remains the coverage anchor.
        status = ResearchGuard(self.store).get(research['id'])
        self.assertEqual(status['status'], 'unresolved')
        self.assertIn('registry is missing or changed', status['reason'])
        self.assertFalse(ResearchGuard(self.store).check(self.store.example_dataset_id, self.unsafe())['allowed'])
        self.assertBlocked(lambda: self.create(self.unsafe(), 'copy-after-deletion'), 'registry is missing or changed')
        # Existing acknowledgments stay recoverable, but cannot create effects.
        self.assertEqual(self.create(self.task, 'original-key'), research)

    def test_changed_older_boundary_dates_cannot_reopen_test_for_new_revision(self):
        research = self.create()
        with transaction(self.store.db_path) as connection:
            # This erased April-June from the old reservation in the previous
            # implementation while leaving its own revision digest untouched.
            connection.execute("UPDATE research_guard_boundaries SET test_start='2023-07-03' WHERE revision_id=?",
                               (research['latest_revision_id'],))
        self.assertBlocked(lambda: self.store.create_revision(research['id'], research['latest_revision_id'],
            self.unsafe(), 'Attempt corrupted-reservation reuse'), 'registry is missing or changed')
        self.assertEqual(ResearchGuard(self.store).get(research['id'])['status'], 'unresolved')

    def test_changed_older_task_is_detected_when_newer_revision_is_submitted(self):
        research = self.create()
        safe = self.store.create_revision(research['id'], research['latest_revision_id'], self.task)
        changed = deepcopy(self.task)
        changed['evaluation']['splits']['test']['start'] = '2023-07-03'
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE revisions SET task=?,digest=? WHERE id=?',
                (json_text(changed), digest(changed), research['latest_revision_id']))
        self.assertBlocked(lambda: self.store.submit_run(safe['id'], 'fixed', 'newer-run'), 'registry is missing or changed')
        self.assertEqual(ResearchGuard(self.store).get(research['id'])['status'], 'unresolved')


if __name__ == '__main__':
    unittest.main()
