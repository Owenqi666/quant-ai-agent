"""Human-reference branches use isolated synthetic labels, never actual ones."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha import semantic_evaluation as core, semantic_materials as materials
from paper_alpha.server import db
from paper_alpha.server.semantic_annotations import SemanticAnnotations
from paper_alpha.server.semantic_evaluation_sets import SemanticEvaluationSets, SCHEMA
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import digest


class SemanticEvaluationSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'workspace')
        with db.transaction(self.store.db_path) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='semantic_evaluation_sets'").fetchone():
                for statement in SCHEMA.split(';'):
                    if statement.strip():
                        connection.execute(statement)
        self.annotations = SemanticAnnotations(self.store)
        self.service = SemanticEvaluationSets(self.store)

    def label(self, *, source='human', case='alpha101_formula', reviewer='Synthetic fixture A; not a real judgment', outcome='passed', key='label', parent=None, change=None):
        dimensions = {name: {'outcome': outcome, 'reason': 'Synthetic declaration fixture, not an actual human assessment.'} for name in materials.DIMENSIONS}
        if change:
            dimensions[change[0]]['outcome'] = change[1]
        return self.annotations.create(material_sha256=materials.MATERIAL_SHA256, case_id=case, source=source,
            reviewer=reviewer, confirmed_at='2020-01-02T00:00:00+00:00' if parent else '2020-01-01T00:00:00+00:00',
            dimensions=dimensions, supersedes_id=parent, idempotency_key=key)

    def request(self, cases=None, key='set'):
        preview = self.service.preview(material_sha256=materials.MATERIAL_SHA256, case_ids=cases or ['alpha101_formula'])
        return {'material_sha256': materials.MATERIAL_SHA256, 'case_ids': preview['case_ids'],
                'expected_active_annotations': preview['active_human_annotations'], 'idempotency_key': key}

    def freeze(self, cases=None, key='set'):
        return self.service.create(**self.request(cases, key))

    def comparison_request(self, reference, key='comparison', outcome='passed'):
        return {'set_id': reference['id'], 'set_digest': reference['digest'], 'source': 'automation',
                'reviewer': 'Synthetic declaration comparison, no provider', 'declared_at': '2020-01-01T00:00:00+00:00',
                'execution_reference': 'Synthetic comparison fixture; no model execution occurred.',
                'case_declarations': [{'case_id': case, 'dimensions': {name: {'outcome': outcome, 'reason': 'Synthetic declared output.'}
                                       for name in materials.DIMENSIONS}} for case in reference['case_ids']], 'idempotency_key': key}

    def test_preview_is_read_only_and_automation_is_pending_not_zero_quality(self):
        label = self.label(source='automation')
        preview = self.service.preview(material_sha256=materials.MATERIAL_SHA256, case_ids=['momentum_window', 'alpha101_formula'])
        self.assertEqual(self.service.list()['total'], 0)
        self.assertEqual(preview['annotation_history'], [label])
        self.assertEqual(preview['active_human_annotations'], [])
        self.assertEqual(preview['summary']['pending_dimensions'], 10)
        reference = self.freeze(['momentum_window', 'alpha101_formula'])
        compared = self.service.compare(**self.comparison_request(reference))
        self.assertEqual(compared['summary']['comparable_dimensions'], 0)
        self.assertIsNone(compared['summary']['declaration_agreement_rate'])
        self.assertIsNone(compared['summary']['semantic_quality_score'])
        self.assertFalse(compared['execution_reference_verified'])
        self.assertEqual(len(self.store._read('SELECT * FROM reviews')), 0)
        self.assertEqual(len(self.store._read('SELECT * FROM research_claims')), 0)

    def test_all_reviewers_frozen_conflict_unknown_and_pending_by_dimension(self):
        first = self.label(change=('field_semantics', 'not_assessed'))
        second = self.label(reviewer='Synthetic fixture B; no actual judgment', key='B', change=('evidence_accuracy', 'failed'))
        automation = self.label(source='automation', key='auto')
        reference = self.freeze(['alpha101_formula', 'wrong_quote'])
        self.assertEqual({item['id'] for item in reference['active_human_annotations']}, {first['id'], second['id']})
        self.assertEqual({item['id'] for item in reference['annotation_history']}, {first['id'], second['id'], automation['id']})
        status = {item['dimension']: item for item in reference['dimension_references'] if item['case_id'] == 'alpha101_formula'}
        self.assertEqual(status['evidence_accuracy']['status'], 'conflicting')
        self.assertEqual(status['field_semantics']['status'], 'conflicting')
        self.assertEqual(status['field_semantics']['unknown_outcomes'], ['not_assessed'])
        self.assertEqual(reference['summary']['eligible_dimensions'], 3)
        self.assertEqual(reference['summary']['pending_dimensions'], 5)
        self.assertEqual(reference['summary']['conflicting_dimensions'], 2)

    def test_unknown_and_not_applicable_are_excluded_not_ground_truth(self):
        self.label(outcome='not_assessed', change=('field_semantics', 'not_applicable'))
        reference = self.freeze()
        self.assertEqual(reference['summary']['unknown_dimensions'], 5)
        comparison = self.service.compare(**self.comparison_request(reference))
        self.assertEqual(comparison['summary']['excluded_reasons'], {'reference_unknown': 5})
        self.assertTrue(all(item['agrees'] is None for item in comparison['dimension_results']))

    def test_cannot_hide_reviewer_or_freeze_stale_active_reference(self):
        first = self.label()
        request = self.request()
        second = self.label(reviewer='Synthetic second reviewer', key='second')
        with self.assertRaises(ServiceError) as caught:
            self.service.create(**request)
        self.assertEqual(caught.exception.status, 409)
        request = self.request(); request['expected_active_annotations'] = [{'id': first['id'], 'digest': first['digest']}]
        with self.assertRaises(ServiceError):
            self.service.create(**request)
        request = self.request(); request['expected_active_annotations'][0]['digest'] = '0' * 64
        with self.assertRaises(ServiceError):
            self.service.create(**request)
        self.assertEqual(self.service.list()['total'], 0)
        self.assertEqual(len(self.request()['expected_active_annotations']), 2)

    def test_new_labels_and_explicit_replacement_do_not_invalidate_old_set(self):
        first = self.label()
        old_request = self.request(); old = self.service.create(**old_request)
        new = self.label(key='replacement', parent=first['id'], outcome='failed')
        other = self.label(case='wrong_quote', key='unrelated')
        self.assertEqual(self.service.get(old['id']), old)
        self.assertEqual(self.service.create(**old_request), old)
        current = self.freeze(key='new-set')
        self.assertNotEqual(current['digest'], old['digest'])
        self.assertEqual(current['annotation_history'], sorted([first, new], key=lambda value: value['id']))
        self.assertEqual(current['active_human_annotations'], [{'id': new['id'], 'digest': new['digest']}])
        self.assertNotIn(other, current['annotation_history'])
        comparison = self.service.compare(**self.comparison_request(old))
        self.assertEqual(comparison['summary']['declaration_agreement_rate'], 1.0)
        self.assertIsNone(comparison['summary']['semantic_quality_score'])

    def test_concurrent_exact_replay_and_changed_key_conflicts(self):
        self.label()
        request = self.request()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.service.create(**request), range(4)))
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(self.service.list()['total'], 1)
        self.assertEqual(len(self.store._read('SELECT * FROM semantic_evaluation_set_receipts')), 1)
        changed = deepcopy(request); changed['case_ids'].append('wrong_quote')
        with self.assertRaises(ServiceError) as caught:
            self.service.create(**changed)
        self.assertEqual(caught.exception.status, 409)
        same_new_key = deepcopy(request); same_new_key['idempotency_key'] = 'new-key-same-reference'
        self.assertEqual(self.service.create(**same_new_key), results[0])
        self.assertEqual(len(self.store._read('SELECT * FROM semantic_evaluation_set_receipts')), 2)

    def test_exact_comparison_counts_all_dimensions_and_excludes_unknown_output(self):
        self.label()
        self.label(case='wrong_quote', key='second-case', outcome='failed')
        reference = self.freeze(['alpha101_formula', 'wrong_quote'])
        request = self.comparison_request(reference)
        request['case_declarations'][0]['dimensions']['field_semantics']['outcome'] = 'not_applicable'
        result = self.service.compare(**request)
        summary = result['summary']
        self.assertEqual((summary['matched_dimensions'], summary['comparable_dimensions'], summary['total_dimensions']), (4, 9, 10))
        self.assertEqual(summary['excluded_reasons'], {'declaration_unknown': 1})
        self.assertAlmostEqual(summary['declaration_agreement_rate'], 4 / 9)
        self.assertIsNone(summary['semantic_quality_score'])
        self.assertEqual(self.service.comparison(result['id']), result)
        self.assertEqual(self.service.comparisons(reference['id'])['items'], [result])

    def test_comparison_wrong_set_missing_duplicate_or_extra_case_and_dimensions_rejected(self):
        reference = self.freeze(['alpha101_formula', 'wrong_quote'])
        variants = []
        request = self.comparison_request(reference); request['set_digest'] = '0' * 64; variants.append(request)
        request = self.comparison_request(reference); request['case_declarations'].pop(); variants.append(request)
        request = self.comparison_request(reference); request['case_declarations'][1] = deepcopy(request['case_declarations'][0]); variants.append(request)
        request = self.comparison_request(reference); request['case_declarations'][1]['case_id'] = 'momentum_window'; variants.append(request)
        request = self.comparison_request(reference); request['case_declarations'][0]['dimensions'].pop('field_semantics'); variants.append(request)
        request = self.comparison_request(reference); request['case_declarations'][0]['dimensions']['new_dimension'] = {}; variants.append(request)
        for request in variants:
            with self.subTest(request=request), self.assertRaises(ServiceError):
                self.service.compare(**request)
        self.assertEqual(self.service.comparisons(reference['id'])['total'], 0)

    def test_comparison_durable_concurrent_replay_and_changed_payload_conflict(self):
        reference = self.freeze()
        request = self.comparison_request(reference)
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _: self.service.compare(**request), range(3)))
        self.assertEqual(results, [results[0]] * 3)
        self.assertEqual(len(self.store._read('SELECT * FROM semantic_evaluation_comparison_receipts')), 1)
        request['execution_reference'] += ' Changed.'
        with self.assertRaises(ServiceError) as caught:
            self.service.compare(**request)
        self.assertEqual(caught.exception.status, 409)

    def test_self_hashed_receipt_cannot_be_retargeted_to_another_creation_request(self):
        reference = self.freeze()
        wrong_request = self.request(['wrong_quote'])
        fingerprint = digest({key: value for key, value in wrong_request.items() if key != 'idempotency_key'})
        receipt = self.store._read('SELECT * FROM semantic_evaluation_set_receipts')[0]
        receipt['request_digest'] = fingerprint
        receipt['receipt_digest'] = digest({key: value for key, value in receipt.items() if key != 'receipt_digest'})
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE semantic_evaluation_set_receipts SET request_digest=?,receipt_digest=?',
                               (receipt['request_digest'], receipt['receipt_digest']))
        for action in (lambda: self.service.create(**wrong_request), lambda: self.service.get(reference['id']),
                       lambda: self.service.list()):
            with self.assertRaises(ServiceError) as caught:
                action()
            self.assertEqual(caught.exception.status, 409)

    def test_self_hashed_comparison_receipt_cannot_be_retargeted_to_changed_declarations(self):
        reference = self.freeze()
        request = self.comparison_request(reference)
        result = self.service.compare(**request)
        changed = deepcopy(request); changed['execution_reference'] = 'Another declared result, not the frozen original.'
        receipt = self.store._read('SELECT * FROM semantic_evaluation_comparison_receipts')[0]
        receipt['request_digest'] = digest({key: value for key, value in changed.items() if key != 'idempotency_key'})
        receipt['receipt_digest'] = digest({key: value for key, value in receipt.items() if key != 'receipt_digest'})
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE semantic_evaluation_comparison_receipts SET request_digest=?,receipt_digest=?',
                               (receipt['request_digest'], receipt['receipt_digest']))
        for action in (lambda: self.service.compare(**changed), lambda: self.service.comparison(result['id']),
                       lambda: self.service.export(reference['id'])):
            with self.assertRaises(ServiceError) as caught:
                action()
            self.assertEqual(caught.exception.status, 409)

    def test_closed_requests_reject_approval_paths_false_literal_and_missing_source(self):
        request = self.request()
        variants = []
        for key, value in [('path', '/etc/passwd'), ('semantic_quality_score', 1), ('identity_verified', True),
                           ('expected_active_annotations', [{'id': 'unknown', 'digest': '0' * 64}]),
                           ('case_ids', ['alpha101_formula', 'alpha101_formula']), ('material_sha256', '0' * 64),
                           ('idempotency_key', ' '), ('case_ids', ['unknown'])]:
            changed = deepcopy(request); changed[key] = value; variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(ServiceError):
                self.service.create(**changed)
        reference = self.freeze()
        for key, value in [('source', None), ('source', 'model'), ('source_path', '/etc/passwd'),
                           ('semantic_quality_score', 1), ('claim_approval', True), ('reviewer', ' '),
                           ('declared_at', '2020-01-01T00:00:00'), ('execution_reference', ' ')]:
            changed = self.comparison_request(reference); changed[key] = value
            with self.subTest(changed=changed), self.assertRaises(ServiceError):
                self.service.compare(**changed)

    def test_original_annotation_tamper_is_detected_after_replacement_and_before_filter(self):
        label = self.label()
        reference = self.freeze()
        self.label(parent=label['id'], key='replace')
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE semantic_annotations SET reviewer=? WHERE id=?', ('hidden by tamper', label['id']))
        for action in (lambda: self.service.get(reference['id']), lambda: self.service.list(),
                       lambda: self.service.preview(material_sha256=materials.MATERIAL_SHA256, case_ids=['wrong_quote'])):
            with self.assertRaises(ServiceError) as caught:
                action()
            self.assertEqual(caught.exception.status, 409)

    def test_set_metadata_payload_receipt_missing_and_comparison_filter_tamper_fail_closed(self):
        reference = self.freeze()
        comparison = self.service.compare(**self.comparison_request(reference))
        other = self.freeze(['wrong_quote'], key='other-set')
        cases = [('semantic_evaluation_sets', 'created_at', reference['id'], '2021-01-01T00:00:00+00:00'),
                 ('semantic_evaluation_sets', 'payload', reference['id'], '[]'),
                 ('semantic_evaluation_comparisons', 'set_id', comparison['id'], reference['id'] + '-hidden'),
                 ('semantic_evaluation_comparisons', 'payload', comparison['id'], '[]')]
        for table, field, identity, value in cases:
            old = self.store._read('SELECT * FROM ' + table + ' WHERE id=?', (identity,))[0]
            with db.transaction(self.store.db_path) as connection:
                connection.execute('PRAGMA defer_foreign_keys=ON')
                # Avoid violating SQLite's FK at commit solely for the corrupt
                # searchable-column branch; point to another real set instead.
                if field == 'set_id':
                    value = other['id']
                connection.execute('UPDATE ' + table + ' SET ' + field + '=? WHERE id=?', (value, identity))
            with self.subTest(table=table, field=field), self.assertRaises(ServiceError):
                self.service.comparisons(reference['id'])
            with db.transaction(self.store.db_path) as connection:
                connection.execute('UPDATE ' + table + ' SET ' + field + '=? WHERE id=?', (old[field], identity))
        with db.transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM semantic_evaluation_set_receipts WHERE set_id=?', (reference['id'],))
        with self.assertRaises(ServiceError):
            self.service.get(reference['id'])

    def test_source_change_in_last_observable_fence_rolls_back_set_and_receipt(self):
        bundle = self.root / 'source-bundle'
        for relative in core.SOURCE_PATHS.values():
            path = bundle / relative; path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(materials.ROOT / relative, path)
        with patch.object(materials, 'ROOT', bundle):
            request = self.request()
            original = self.service._insert_receipt
            def race(*args, **kwargs):
                original(*args, **kwargs)
                with (bundle / core.SOURCE_PATHS['momentum_source_registry']).open('a') as stream:
                    stream.write(' ')
            with patch.object(self.service, '_insert_receipt', side_effect=race), self.assertRaises(ServiceError):
                self.service.create(**request)
        self.assertEqual(self.service.list()['total'], 0)
        self.assertEqual(len(self.store._read('SELECT * FROM semantic_evaluation_set_receipts')), 0)

    def test_source_change_rolls_back_comparison_and_restored_source_keeps_reference(self):
        bundle = self.root / 'source-bundle'
        for relative in core.SOURCE_PATHS.values():
            path = bundle / relative; path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(materials.ROOT / relative, path)
        with patch.object(materials, 'ROOT', bundle):
            reference = self.freeze()
            source = bundle / core.SOURCE_PATHS['momentum_source_registry']
            original_bytes = source.read_bytes()
            original = self.service._insert_receipt
            def race(*args, **kwargs):
                original(*args, **kwargs)
                source.write_bytes(original_bytes + b' ')
            with patch.object(self.service, '_insert_receipt', side_effect=race), self.assertRaises(ServiceError):
                self.service.compare(**self.comparison_request(reference))
            source.write_bytes(original_bytes)
            self.assertEqual(self.service.get(reference['id']), reference)
        self.assertEqual(self.service.comparisons(reference['id'])['total'], 0)
        self.assertEqual(len(self.store._read('SELECT * FROM semantic_evaluation_comparison_receipts')), 0)

    def test_nontext_payload_and_corrupted_receipt_replay_fail_closed(self):
        reference = self.freeze()
        comparison = self.service.compare(**self.comparison_request(reference))
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE semantic_evaluation_comparisons SET payload=? WHERE id=?', (b'[]', comparison['id']))
        with self.assertRaises(ServiceError) as caught:
            self.service.compare(**self.comparison_request(reference))
        self.assertEqual(caught.exception.status, 409)
        # Keep this corruption, but a direct read must also fail as a controlled
        # integrity rejection rather than leaking AttributeError.
        with self.assertRaises(ServiceError):
            self.service.comparison(comparison['id'])

    def test_aggregate_byte_budget_rejects_before_fetch_and_rolls_back_new_resource(self):
        from paper_alpha.server import semantic_evaluation_sets as module
        reference = self.freeze()
        used = self.store._read('SELECT length(CAST(payload AS BLOB)) AS bytes FROM semantic_evaluation_sets')[0]['bytes']
        with patch.object(module, 'MAX_LEDGER_BYTES', used - 1), self.assertRaises(ServiceError) as caught:
            self.service.list()
        self.assertEqual(caught.exception.status, 413)
        with patch.object(module, 'MAX_LEDGER_BYTES', used + 1), self.assertRaises(ServiceError) as caught:
            self.service.compare(**self.comparison_request(reference))
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(self.service.comparisons(reference['id'])['total'], 0)
        self.assertEqual(len(self.store._read('SELECT * FROM semantic_evaluation_comparison_receipts')), 0)

    def test_offline_bundle_integrity_sources_annotations_summary_manifest_and_metadata(self):
        self.label()
        reference = self.freeze()
        self.service.compare(**self.comparison_request(reference))
        bundle = self.service.export(reference['id'])
        verified = core.verify_bundle(bundle, root=materials.ROOT)
        self.assertTrue(verified['source_bytes_reverified'])
        self.assertFalse(verified['identity_verified'])
        self.assertTrue(core.verify_bundle(bundle)['passed'])
        variants = []
        for target in ('material', 'annotation', 'summary', 'source', 'metadata', 'comparison', 'manifest'):
            changed = deepcopy(bundle)
            if target == 'material': changed['reference']['case_snapshots'][0]['reference_draft'] += ' Changed.'
            elif target == 'annotation': changed['reference']['annotation_history'][0]['reviewer'] += ' Changed.'
            elif target == 'summary': changed['reference']['summary']['eligible_dimensions'] = 0
            elif target == 'source': changed['reference']['source_snapshots'][1]['content'] += ' '
            elif target == 'metadata': changed['reference']['created_at'] = '2021-01-01T00:00:00+00:00'
            elif target == 'comparison': changed['comparisons'][0]['summary']['matched_dimensions'] = 0
            else: changed['manifest_digest'] = '0' * 64
            # Rehashing the outer wrapper must not conceal changed internal
            # immutable digests or the source-bound derived projections.
            if target != 'manifest': changed['manifest_digest'] = digest({key: value for key, value in changed.items() if key != 'manifest_digest'})
            variants.append((target, changed))
        for name, changed in variants:
            with self.subTest(name=name), self.assertRaises(ValueError):
                core.verify_bundle(changed)
        source_root = self.root / 'local-sources'
        for relative in core.SOURCE_PATHS.values():
            path = source_root / relative; path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(materials.ROOT / relative, path)
        (source_root / 'examples/alpha101/paper.pdf').write_bytes(b'Changed binary source')
        with self.assertRaises(ValueError):
            core.verify_bundle(bundle, root=source_root)

    def test_offline_rehashed_bad_derivation_and_automation_promoted_to_human_rejected(self):
        self.label(source='automation')
        reference = self.freeze()
        bad = deepcopy(reference)
        bad['active_human_annotations'] = [{'id': bad['annotation_history'][0]['id'], 'digest': bad['annotation_history'][0]['digest']}]
        bad['digest'] = digest({key: bad[key] for key in core.SET_FIELDS})
        bad['id'] = 'semantic_evaluation_set_' + bad['digest']
        with self.assertRaises(ValueError):
            core.verify_detail(bad)
        reference = self.freeze(['wrong_quote'], key='other')
        request = self.comparison_request(reference)
        comparison = self.service.compare(**request)
        bad = deepcopy(comparison)
        bad['summary']['declaration_agreement_rate'] = 1.0
        bad['digest'] = digest({key: bad[key] for key in core.COMPARISON_FIELDS})
        bad['id'] = 'semantic_evaluation_comparison_' + bad['digest']
        with self.assertRaises(ValueError):
            core.verify_comparison(bad, reference)

    def test_snapshot_history_retains_parent_and_independent_verification_rejects_missing_parent(self):
        first = self.label()
        self.label(parent=first['id'], key='replacement')
        reference = self.freeze()
        self.assertTrue(core.verify_detail(reference))
        bad = deepcopy(reference)
        bad['annotation_history'] = [item for item in bad['annotation_history'] if item['id'] != first['id']]
        with self.assertRaises(ValueError):
            core.verify_snapshot({key: bad[key] for key in core.SET_FIELDS})

    def test_offline_history_bound_counts_root_and_endpoint_as_records(self):
        first = self.label(source='automation')
        history = [first]
        while len(history) < 100:
            previous = history[-1]
            body = {key: deepcopy(value) for key, value in previous.items() if key not in {'id', 'digest', 'created_at'}}
            body.update(supersedes_id=previous['id'], supersedes_digest=previous['digest'])
            fingerprint = digest(body)
            history.append({**body, 'id': 'semantic_annotation_' + fingerprint, 'digest': fingerprint, 'created_at': first['created_at']})
        material, sources = materials.load_material(), core.sources_snapshot()
        bounded = core.build_snapshot(['alpha101_formula'], material, history, sources)
        self.assertEqual(bounded['summary']['automation_audit_records'], 100)
        body = {key: deepcopy(value) for key, value in history[-1].items() if key not in {'id', 'digest', 'created_at'}}
        body.update(supersedes_id=history[-1]['id'], supersedes_digest=history[-1]['digest'])
        fingerprint = digest(body)
        history.append({**body, 'id': 'semantic_annotation_' + fingerprint, 'digest': fingerprint, 'created_at': first['created_at']})
        with self.assertRaises(ValueError):
            core.build_snapshot(['alpha101_formula'], material, history, sources)

    def test_export_and_verify_cli_are_read_only_and_explicit_destination(self):
        reference = self.freeze()
        destination = self.root / 'export.json'
        before = self.store._read('SELECT * FROM semantic_evaluation_set_receipts')
        exported = subprocess.run([sys.executable, 'scripts/export_semantic_set.py', '--workspace', str(self.store.root), '--set-id', reference['id'], '--out', str(destination)], text=True, capture_output=True)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        verified = subprocess.run([sys.executable, 'scripts/verify_semantic_set.py', str(destination)], text=True, capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)['passed'])
        self.assertEqual(before, self.store._read('SELECT * FROM semantic_evaluation_set_receipts'))
        repeated = subprocess.run([sys.executable, 'scripts/export_semantic_set.py', '--workspace', str(self.store.root), '--set-id', reference['id'], '--out', str(destination)], text=True, capture_output=True)
        self.assertNotEqual(repeated.returncode, 0)
        duplicate = self.root / 'duplicate.json'; duplicate.write_text('{"schema_version":1,"schema_version":1}')
        malformed = subprocess.run([sys.executable, 'scripts/verify_semantic_set.py', str(duplicate)], text=True, capture_output=True)
        self.assertNotEqual(malformed.returncode, 0)


if __name__ == '__main__':
    unittest.main()
