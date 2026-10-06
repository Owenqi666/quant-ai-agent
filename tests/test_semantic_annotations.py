"""Synthetic declaration branches only; never actual human judgments."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha import semantic_materials as materials
from paper_alpha.server import db
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.semantic_annotations import SemanticAnnotations, SCHEMA
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import read_json


class SemanticAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root / 'workspace')
        # During agent development schema integration belongs to the root agent.
        # Once registered by schema15 this branch is a no-op.
        with db.transaction(self.store.db_path) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_annotations'").fetchone():
                for statement in SCHEMA.split(';'):
                    if statement.strip():
                        connection.execute(statement)
        self.annotations = SemanticAnnotations(self.store)

    def request(self, source='automation', case='alpha101_formula', reviewer='Synthetic test fixture; not an actual human judgment', key='annotation'):
        return {'material_sha256': materials.MATERIAL_SHA256, 'case_id': case, 'source': source,
                'reviewer': reviewer, 'confirmed_at': '2020-01-01T00:00:00+00:00',
                'dimensions': {name: {'outcome': 'not_assessed', 'reason': 'Synthetic parser/recovery test only.'}
                               for name in materials.DIMENSIONS}, 'supersedes_id': None, 'idempotency_key': key}

    def test_material_sources_are_whitelisted_and_pending_is_not_zero_quality(self):
        collection = self.annotations.materials()
        self.assertEqual(collection['total'], 7)
        alpha = self.annotations.material('alpha101_formula')
        self.assertEqual(alpha['evidence'][0]['source_id'], 'alpha101_paper')
        self.assertTrue(alpha['sources'][0]['raw_pdf_reverified'])
        momentum = self.annotations.material('momentum_window')
        self.assertFalse(momentum['sources'][0]['raw_pdf_reverified'])
        self.assertEqual(momentum['reference_status'], 'unverified_reference_draft')
        path, media = self.annotations.source_file('alpha101_paper')
        self.assertEqual(media, 'application/pdf'); self.assertEqual(path, materials.ROOT / 'examples/alpha101/paper.pdf')
        for identity in ('../secret', 'file:///etc/passwd', 'artifacts/research-momentum-01/paper.pdf'):
            with self.assertRaises(ServiceError):
                self.annotations.source_file(identity)
        summary = self.annotations.summary()
        self.assertEqual(summary['pending_cases'], 7)
        self.assertEqual(summary['human_records'], 0)
        self.assertIsNone(summary['semantic_quality_score'])
        self.assertEqual(summary['status'], 'pending_human_confirmation')
        self.assertTrue(all(len(row['unknown_dimensions']) == 5 for row in summary['cases']))

    def test_preview_no_write_and_automation_never_creates_human_coverage_or_approval(self):
        request = self.request()
        preview = self.annotations.preview(**{k: v for k, v in request.items() if k != 'idempotency_key'})
        self.assertEqual(self.annotations.list()['total'], 0)
        value = self.annotations.create(**request)
        for key in ('original_result_approval', 'claim_approval', 'software_verified_semantic_truth'):
            self.assertFalse(value[key]); self.assertFalse(preview[key])
        summary = self.annotations.summary()
        self.assertEqual(summary['automation_records'], 1)
        self.assertEqual(summary['human_records'], 0)
        self.assertEqual(summary['pending_cases'], 7)
        self.assertEqual(self.annotations.get(value['id']), value)
        self.assertEqual(read_json_text(self.annotations.export(value['id'])), value)
        self.assertEqual(len(self.store._read('SELECT * FROM reviews')), 0)
        self.assertEqual(len(self.store._read('SELECT * FROM research_claims')), 0)

    def test_invalid_explicit_fields_and_approval_injection_are_rejected(self):
        variants = []
        for field in ('source', 'reviewer', 'confirmed_at', 'dimensions'):
            value = self.request(); del value[field]; variants.append(value)
        for field, injected in [('material_sha256', '0' * 64), ('case_id', 'unknown'), ('source', 'model'),
                                ('reviewer', ' '), ('confirmed_at', '2020-01-01T00:00:00'),
                                ('confirmed_at', '2999-01-01T00:00:00+00:00'), ('idempotency_key', ' '),
                                ('claim_approval', True), ('semantic_quality_score', 1), ('source_path', '/etc/passwd')]:
            value = self.request(); value[field] = injected; variants.append(value)
        value = self.request(); value['dimensions'].pop('field_semantics'); variants.append(value)
        value = self.request(); value['dimensions']['field_semantics']['reason'] = ' '; variants.append(value)
        value = self.request(); value['dimensions']['field_semantics']['outcome'] = True; variants.append(value)
        value = self.request(); value['dimensions']['fake'] = {}; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ServiceError):
                self.annotations.create(**value)
        self.assertEqual(self.annotations.list()['total'], 0)

    def test_concurrent_response_replay_is_one_record_and_changed_payload_conflicts(self):
        request = self.request()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.annotations.create(**request), range(4)))
        self.assertTrue(all(row == results[0] for row in results))
        self.assertEqual(self.annotations.list()['total'], 1)
        self.assertEqual(len(self.store._read('SELECT * FROM semantic_annotation_receipts')), 1)
        altered = deepcopy(request); altered['dimensions']['evidence_accuracy']['reason'] += ' Changed.'
        with self.assertRaises(ServiceError) as caught:
            self.annotations.create(**altered)
        self.assertEqual(caught.exception.status, 409)
        altered['idempotency_key'] = 'newkey-without-replacement'
        with self.assertRaises(ServiceError) as caught:
            self.annotations.create(**altered)
        self.assertEqual(caught.exception.status, 409)

    def test_explicit_replacement_retains_history_and_replay_of_old_key(self):
        request = self.request('human')
        old = self.annotations.create(**request)
        replacement = deepcopy(request); replacement.update(supersedes_id=old['id'], idempotency_key='replacement', confirmed_at='2020-01-02T00:00:00+00:00')
        for value in replacement['dimensions'].values():
            value.update(outcome='passed', reason='Synthetic positive branch fixture, not a real human label.')
        new = self.annotations.create(**replacement)
        self.assertEqual(new['supersedes_digest'], old['digest'])
        self.assertEqual(self.annotations.get(old['id']), old)
        self.assertEqual(self.annotations.create(**request), old)
        self.assertEqual(self.annotations.list()['total'], 2)
        summary = self.annotations.summary()
        self.assertEqual(summary['human_records'], 2)
        self.assertEqual(summary['active_human_records'], 1)
        self.assertEqual(summary['superseded_records'], 1)
        case = next(item for item in summary['cases'] if item['case_id'] == 'alpha101_formula')
        self.assertEqual(case['active_human_annotation_ids'], [new['id']])
        self.assertEqual(case['human_declared_status'], 'passed')
        self.assertIsNone(summary['semantic_quality_score'])

    def test_replacement_scope_time_and_branching_are_rejected(self):
        old = self.annotations.create(**self.request())
        variants = []
        for field, value in [('case_id', 'wrong_quote'), ('reviewer', 'different fixture'), ('source', 'human'),
                             ('confirmed_at', '2019-01-01T00:00:00+00:00')]:
            request = self.request(key='new'); request['supersedes_id'] = old['id']; request[field] = value; variants.append(request)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ServiceError):
                self.annotations.create(**value)
        def replace(key):
            request = self.request(key=key); request['supersedes_id'] = old['id']
            try:
                return self.annotations.create(**request)
            except ServiceError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(replace, ('first', 'second')))
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertIn(409, results)
        self.assertEqual(self.annotations.list()['total'], 2)

    def test_distinct_reviewers_conflict_is_explicit_and_reason_variation_is_not_conflict(self):
        request = self.request('human', reviewer='Synthetic reviewer A; fixture only')
        for dimension in request['dimensions'].values():
            dimension['outcome'] = 'passed'
        self.annotations.create(**request)
        second = self.request('human', reviewer='Synthetic reviewer B; fixture only', key='B')
        second['dimensions'] = deepcopy(request['dimensions'])
        second['dimensions']['evidence_accuracy']['reason'] = 'Another fixture explanation.'
        self.annotations.create(**second)
        case = self.annotations.summary()['cases'][0]
        self.assertEqual(case['human_declared_status'], 'passed')
        third = self.request('human', reviewer='Synthetic reviewer C; fixture only', key='C')
        third['dimensions'] = deepcopy(request['dimensions'])
        third['dimensions']['evidence_accuracy']['outcome'] = 'failed'
        self.annotations.create(**third)
        summary = self.annotations.summary()
        self.assertEqual(summary['conflicting_cases'], 1)
        self.assertEqual(summary['cases'][0]['human_declared_status'], 'conflicting')
        self.assertIn('evidence_accuracy', summary['cases'][0]['unknown_dimensions'])
        self.assertEqual(self.annotations.summary(reviewer=request['reviewer'])['conflicting_cases'], 0)

    def test_material_source_and_symlink_tampering_fail_closed(self):
        bundle = self.root / 'bundle'
        for relative in ('evaluation_suites/v018/semantic_cases.json', 'evaluation_suites/v018/manifest.json',
                         *[value[0] for value in materials.SOURCE_FILES.values()]):
            target = bundle / relative; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(materials.ROOT / relative, target)
        self.assertEqual(len(materials.load_material(root=bundle)['cases']), 7)
        value = self.annotations.create(**self.request())
        source = bundle / 'docs/research/momentum_sources.json'
        original = source.read_bytes(); source.write_bytes(original + b' ')
        with patch.object(materials, 'ROOT', bundle):
            for call in (self.annotations.materials, lambda: self.annotations.get(value['id']), self.annotations.summary):
                with self.assertRaises(ServiceError) as caught:
                    call()
                self.assertEqual(caught.exception.status, 409)
        source.write_bytes(original)
        material = bundle / 'evaluation_suites/v018/semantic_cases.json'
        original_material = material.read_bytes(); material.write_bytes(original_material + b' ')
        with self.assertRaises(ValueError):
            materials.load_material(root=bundle)
        material.write_bytes(original_material)
        source.unlink(); source.symlink_to(materials.ROOT / 'docs/research/momentum_sources.json')
        with self.assertRaises(ValueError):
            materials.load_material(root=bundle)

    def test_row_metadata_receipt_missing_receipt_and_missing_parent_are_detected(self):
        for target in ('payload', 'metadata', 'receipt', 'missing_receipt', 'missing_parent'):
            with self.subTest(target=target):
                # Roll back tampering after each isolated adverse check.
                value = self.annotations.create(**self.request(key=target, reviewer='Synthetic ' + target))
                with closing(db.connect(self.store.db_path)) as connection:
                    connection.execute('PRAGMA foreign_keys=OFF'); connection.execute('BEGIN')
                    if target == 'payload':
                        connection.execute("UPDATE semantic_annotations SET payload=payload||' ' WHERE id=?", (value['id'],))
                    elif target == 'metadata':
                        connection.execute("UPDATE semantic_annotations SET reviewer='Changed' WHERE id=?", (value['id'],))
                    elif target == 'receipt':
                        connection.execute("UPDATE semantic_annotation_receipts SET request_digest=? WHERE annotation_id=?", ('0' * 64, value['id']))
                    elif target == 'missing_receipt':
                        connection.execute('DELETE FROM semantic_annotation_receipts WHERE annotation_id=?', (value['id'],))
                    else:
                        connection.execute('DELETE FROM semantic_annotations WHERE id=?', (value['id'],))
                    # Same connection sees uncommitted corruption for direct
                    # verifier checks; public checks are covered below as well.
                    row = connection.execute('SELECT * FROM semantic_annotations WHERE id=?', (value['id'],)).fetchone()
                    with self.assertRaises(ServiceError):
                        self.annotations._ledger_integrity(connection) if target in {'receipt', 'missing_receipt', 'missing_parent'} else self.annotations._record(connection, row)
                    connection.rollback()
                self.assertEqual(self.annotations.get(value['id']), value)

    def test_public_reads_and_replay_reject_committed_row_tampering(self):
        request = self.request()
        value = self.annotations.create(**request)
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE semantic_annotations SET payload=payload||' ' WHERE id=?", (value['id'],))
        for call in (lambda: self.annotations.get(value['id']), self.annotations.list, self.annotations.summary,
                     lambda: self.annotations.create(**request), lambda: self.annotations.export(value['id'])):
            with self.assertRaises(ServiceError) as caught:
                call()
            self.assertEqual(caught.exception.status, 409)

    def test_corrupt_searchable_columns_cannot_hide_filtered_history_or_allow_new_writes(self):
        request = self.request()
        self.annotations.create(**request)
        with db.transaction(self.store.db_path) as connection:
            connection.execute("UPDATE semantic_annotations SET reviewer='Changed filter identity'")
        new_request = self.request(case='wrong_quote', key='new-case')
        for call in (lambda: self.annotations.list(reviewer=request['reviewer']),
                     lambda: self.annotations.list(source='human'),
                     lambda: self.annotations.list(case_id='wrong_quote'),
                     lambda: self.annotations.summary(reviewer=request['reviewer']),
                     lambda: self.annotations.preview(**{k: v for k, v in new_request.items() if k != 'idempotency_key'}),
                     lambda: self.annotations.create(**new_request)):
            with self.assertRaises(ServiceError) as caught:
                call()
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.store._read('SELECT * FROM semantic_annotations')), 1)

    def test_source_change_after_record_verification_rolls_back_creation_and_receipt(self):
        bundle = self.root / 'bundle-fence'
        for relative in ('evaluation_suites/v018/semantic_cases.json', 'evaluation_suites/v018/manifest.json',
                         *[value[0] for value in materials.SOURCE_FILES.values()]):
            target = bundle / relative; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(materials.ROOT / relative, target)
        source = bundle / 'docs/research/momentum_sources.json'
        original_record = self.annotations._record
        def change_source_after_verification(*args, **kwargs):
            value = original_record(*args, **kwargs)
            source.write_bytes(source.read_bytes() + b' ')
            return value
        with patch.object(materials, 'ROOT', bundle), patch.object(self.annotations, '_record', change_source_after_verification):
            with self.assertRaises(ServiceError) as caught:
                self.annotations.create(**self.request())
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.annotations.list()['total'], 0)
        self.assertEqual(self.store._read('SELECT * FROM semantic_annotation_receipts'), [])

    def test_bounded_pagination_and_filters(self):
        self.annotations.create(**self.request())
        self.assertEqual(self.annotations.list(source='human')['total'], 0)
        self.assertEqual(self.annotations.list(source='automation', case_id='alpha101_formula')['total'], 1)
        self.assertEqual(self.annotations.list(limit=1, offset=1)['items'], [])
        for kwargs in ({'limit': True}, {'limit': 101}, {'offset': -1}, {'source': 'fake'}, {'reviewer': ' '}, {'case_id': 'unknown'}):
            with self.assertRaises(ServiceError):
                self.annotations.list(**kwargs)

    def test_backup_restore_retains_exact_annotations_replacements_and_receipts(self):
        request = self.request()
        parent = self.annotations.create(**request)
        replacement = self.request(key='replacement')
        replacement['supersedes_id'] = parent['id']
        child = self.annotations.create(**replacement)
        original_summary = self.annotations.summary()
        backup, restored = self.root / 'backup', self.root / 'restored'
        create_backup(self.store.root, backup)
        restore_backup(backup, restored)
        service = SemanticAnnotations(Store(restored))
        self.assertEqual(service.get(parent['id']), parent)
        self.assertEqual(service.get(child['id']), child)
        self.assertEqual(service.create(**request), parent)
        self.assertEqual(service.create(**replacement), child)
        self.assertEqual(service.summary(), original_summary)


def read_json_text(value):
    import json
    return json.loads(value)


if __name__ == '__main__':
    unittest.main()
