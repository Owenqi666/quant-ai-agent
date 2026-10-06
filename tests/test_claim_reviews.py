"""Exact-claim review controls; human-shaped fixtures are never real labels."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha import eligibility
from paper_alpha.server import claim_reviews, db
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.claim_reviews import ClaimReviews
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import digest, json_text
from paper_alpha.workflow import run_task


class ClaimReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'workspace')
        self.reviews = ClaimReviews(self.store)
        self.claims = ResearchClaims(self.store)
        self.cases = ResearchCases(self.store)
        self.study = AuthorStudies(self.store).create('Synthetic source counts', 'Not raw-source authenticated.',
            eligibility.demo_scan(), None, [], 'study')
        preview = self.cases.preview('author_study', self.study['id'])
        self.case = self.cases.create('Synthetic counts context', '', 'author_study', self.study['id'], preview['source_digest'], 'case')
        result = self.case['context']['results'][0]
        self.draft = {'id': 'count', 'kind': 'metric', 'attribution': 'project_convention',
                      'text': 'The untrusted narrative incorrectly claims count 999999.', 'evidence_ids': [],
                      'metric_references': [{'case_id': self.case['id'], 'case_digest': self.case['digest'],
                          'result_id': result['id'], 'result_digest': result['digest'], 'pointer': '/summary/min_selected'}]}
        self.batch = self.claims.create(self.case['id'], self.case['digest'], [self.draft], 'claims')

    def request(self, source='automation', reviewer='Synthetic fixture; not a real human judgment', key='review', batch=None, claim_id='count', outcome='not_assessed'):
        batch = batch or self.batch
        target = self.reviews.target(batch['id'], claim_id)
        return {'claims_id': batch['id'], 'claim_id': claim_id, 'expected_target_digest': target['target_digest'],
                'source': source, 'reviewer': reviewer, 'confirmed_at': '2020-01-01T00:00:00+00:00',
                'dimensions': {name: {'outcome': outcome, 'reason': 'Synthetic isolated parser/control fixture; not an observed human judgment.'} for name in DIMENSIONS},
                'supersedes_id': None, 'expected_supersedes_digest': None, 'idempotency_key': key}

    def preview(self, request):
        return self.reviews.preview(**{key: value for key, value in request.items() if key != 'idempotency_key'})

    def assert_error(self, callback, status=None):
        with self.assertRaises(ServiceError) as caught:
            callback()
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def test_target_binds_exact_wording_source_evidence_and_real_tool_counts(self):
        target = self.reviews.target(self.batch['id'], 'count')
        self.assertEqual(target['claims_digest'], self.batch['digest'])
        self.assertEqual(target['claim_digest'], digest(self.batch['claims'][0]))
        self.assertEqual(target['case_context_digest'], digest(self.case['context']))
        self.assertEqual(target['source_id'], self.study['id'])
        self.assertEqual(target['evidence'], self.case['context']['evidence'])
        self.assertEqual(target['provenance'], self.case['context']['provenance'])
        self.assertEqual(target['claim']['metrics'][0]['value'], self.study['result']['summary']['min_selected'])
        self.assertEqual(target['claim']['metrics'][0]['verification'], 'verified_result_value')
        self.assertIn('999999', target['claim']['narrative_text'])
        self.assertNotIn('999999', target['claim']['authoritative_display'])
        self.assertEqual(target['claim']['semantic_fidelity'], 'unverified')
        self.assertEqual(target['target_digest'], digest({key: value for key, value in target.items() if key != 'target_digest'}))
        self.assert_error(lambda: self.reviews.target(self.batch['id'], 'unknown'), 404)
        self.assert_error(lambda: self.reviews.target('../private', 'count'), 422)

    def test_preview_is_read_only_and_automation_passes_never_become_human_approval(self):
        request = self.request(outcome='passed')
        preview = self.preview(request)
        self.assertEqual(self.reviews.list()['total'], 0)
        created = self.reviews.create(**request)
        status = self.reviews.status(self.batch['id'], 'count')
        self.assertEqual(preview['declared_semantic_status'], 'passed')
        self.assertEqual(status['human_declared_status'], 'pending')
        self.assertEqual(status['automation_records'], 1); self.assertEqual(status['human_records'], 0)
        self.assertEqual(status['unknown_dimensions'], list(DIMENSIONS))
        self.assertIsNone(status['semantic_quality_score'])
        self.assertFalse(created['original_result_approval']); self.assertFalse(created['software_verified_semantic_truth'])
        self.assertEqual(self.claims.get(self.batch['id']), self.batch)
        self.assertEqual(json.loads(self.reviews.export(created['id'])), created)
        self.assertEqual(AuthorStudies(self.store).reviews(self.study['id']), {'items': []})

    def test_explicit_fields_and_forged_values_approval_or_provenance_are_rejected(self):
        variants = []
        for key in ('expected_target_digest', 'source', 'reviewer', 'confirmed_at', 'dimensions'):
            request = self.request(); del request[key]; variants.append(request)
        for key, value in [('source', 'model'), ('reviewer', ' '), ('claim_id', ' '),
                           ('confirmed_at', '2020-01-01T00:00:00'), ('confirmed_at', '2999-01-01T00:00:00+00:00'),
                           ('idempotency_key', ' '), ('value', 999), ('approved', True), ('target', {}),
                           ('provenance', []), ('semantic_quality_score', 1)]:
            request = self.request(); request[key] = value; variants.append(request)
        request = self.request(); del request['dimensions']['field_semantics']; variants.append(request)
        request = self.request(); request['dimensions']['field_semantics']['reason'] = ' '; variants.append(request)
        request = self.request(); request['dimensions']['field_semantics']['outcome'] = True; variants.append(request)
        request = self.request(); request['supersedes_id'] = 'claim_review_' + '0' * 64; variants.append(request)
        request = self.request(); request['expected_supersedes_digest'] = '0' * 64; variants.append(request)
        for request in variants:
            with self.subTest(keys=request.keys()):
                self.assert_error(lambda: self.reviews.create(**request), 422)
        self.assertEqual(self.reviews.list()['total'], 0)

    def test_stale_target_cannot_approve_reworded_or_extended_batch(self):
        old = self.reviews.create(**self.request('human', outcome='passed'))
        draft = deepcopy(self.draft); draft['text'] = 'Reworded exact claim; no approval inheritance.'
        changed = self.claims.create(self.case['id'], self.case['digest'], [draft], 'changed')
        request = self.request('human', batch=changed, key='new')
        request['expected_target_digest'] = old['target']['target_digest']
        self.assert_error(lambda: self.reviews.create(**request), 409)
        self.assertEqual(self.reviews.status(changed['id'], 'count')['human_declared_status'], 'pending')
        # The same exact text in a different complete batch is also a new target.
        added = deepcopy(self.draft); added['id'] = 'additional'
        extended = self.claims.create(self.case['id'], self.case['digest'], [self.draft, added], 'extended')
        self.assertNotEqual(self.reviews.target(extended['id'], 'count')['target_digest'], old['target']['target_digest'])
        self.assertEqual(self.reviews.status(extended['id'], 'count')['human_records'], 0)

    def test_concurrent_lost_response_replay_and_changed_request_conflict(self):
        request = self.request()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.reviews.create(**request), range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.reviews.list()['total'], 1)
        self.assertEqual(len(self.store._read('SELECT * FROM claim_review_receipts')), 1)
        changed = deepcopy(request); changed['dimensions']['evidence_accuracy']['reason'] += ' Changed.'
        self.assert_error(lambda: self.reviews.create(**changed), 409)
        changed['idempotency_key'] = 'new-key-without-supersession'
        self.assert_error(lambda: self.reviews.create(**changed), 409)

    def test_explicit_exact_replacement_retains_history_and_replays_original(self):
        request = self.request('human')
        first = self.reviews.create(**request)
        replacement = self.request('human', key='replacement', outcome='passed')
        replacement.update(supersedes_id=first['id'], expected_supersedes_digest=first['digest'], confirmed_at='2020-01-02T00:00:00+00:00')
        second = self.reviews.create(**replacement)
        self.assertEqual(second['supersedes_digest'], first['digest'])
        self.assertEqual(self.reviews.get(first['id']), first)
        self.assertEqual(self.reviews.create(**request), first)
        status = self.reviews.status(self.batch['id'], 'count')
        self.assertEqual(status['superseded_records'], 1)
        self.assertEqual(status['active_human_review_ids'], [second['id']])
        self.assertEqual(status['human_declared_status'], 'passed')

    def test_replacement_wrong_digest_cross_scope_time_and_branching_are_rejected(self):
        first = self.reviews.create(**self.request('human'))
        base = self.request('human', key='next')
        base.update(supersedes_id=first['id'], expected_supersedes_digest=first['digest'])
        for key, value, status in [('expected_supersedes_digest', '0' * 64, 409), ('source', 'automation', 422),
                                    ('reviewer', 'Different synthetic reviewer', 422), ('confirmed_at', '2019-01-01T00:00:00+00:00', 422)]:
            request = deepcopy(base); request[key] = value
            self.assert_error(lambda: self.reviews.create(**request), status)
        draft = deepcopy(self.draft); draft['text'] = 'Another exact version.'
        batch = self.claims.create(self.case['id'], self.case['digest'], [draft], 'foreign-target')
        request = self.request('human', batch=batch, key='foreign')
        request.update(supersedes_id=first['id'], expected_supersedes_digest=first['digest'])
        self.assert_error(lambda: self.reviews.create(**request), 422)
        self.reviews.create(**base)
        branch = deepcopy(base); branch['idempotency_key'] = 'branch'
        self.assert_error(lambda: self.reviews.create(**branch), 409)

    def test_two_concurrent_replacements_cannot_branch(self):
        first = self.reviews.create(**self.request())
        requests = []
        for index in range(2):
            request = self.request(key=f'branch-{index}')
            request.update(supersedes_id=first['id'], expected_supersedes_digest=first['digest'])
            request['dimensions']['evidence_accuracy']['reason'] += str(index)
            requests.append(request)
        def commit(request):
            try:
                return self.reviews.create(**request)
            except ServiceError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(commit, requests))
        self.assertEqual(sum(isinstance(output, dict) for output in outputs), 1)
        self.assertIn(409, outputs)
        self.assertEqual(self.reviews.list()['total'], 2)

    def test_different_human_reviewers_conflict_explicitly_and_reasons_do_not(self):
        self.reviews.create(**self.request('human', reviewer='Synthetic reviewer A', key='A', outcome='passed'))
        request = self.request('human', reviewer='Synthetic reviewer B', key='B', outcome='passed')
        request['dimensions']['evidence_accuracy']['reason'] += ' Different reason, same outcome.'
        self.reviews.create(**request)
        self.assertEqual(self.reviews.status(self.batch['id'], 'count')['human_declared_status'], 'passed')
        request = self.request('human', reviewer='Synthetic reviewer C', key='C', outcome='passed')
        request['dimensions']['hypothesis_fidelity']['outcome'] = 'failed'
        self.reviews.create(**request)
        status = self.reviews.status(self.batch['id'], 'count')
        self.assertEqual(status['human_declared_status'], 'conflicting')
        self.assertEqual(status['unknown_dimensions'], ['hypothesis_fidelity'])

    def test_not_applicable_and_unassessed_remain_incomplete(self):
        request = self.request('human', outcome='passed')
        request['dimensions']['implementation_alignment']['outcome'] = 'not_applicable'
        self.reviews.create(**request)
        status = self.reviews.status(self.batch['id'], 'count')
        self.assertEqual(status['human_declared_status'], 'incomplete')
        self.assertEqual(status['unknown_dimensions'], ['implementation_alignment'])

    def test_corrupt_search_columns_cannot_hide_history_or_allow_new_writes(self):
        created = self.reviews.create(**self.request())
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE claim_reviews SET reviewer=? WHERE id=?', ('Hidden forged reviewer', created['id']))
        request = self.request(key='another', reviewer='Other explicit synthetic reviewer')
        callbacks = [lambda: self.reviews.list(reviewer='Synthetic fixture; not a real human judgment'),
                     lambda: self.reviews.get(created['id']), lambda: self.reviews.status(self.batch['id'], 'count'),
                     lambda: self.reviews.export(created['id']), lambda: self.preview(request), lambda: self.reviews.create(**request)]
        for callback in callbacks:
            self.assert_error(callback, 409)

    def test_payload_metadata_receipt_and_missing_receipt_are_detected(self):
        for damage in ('payload', 'metadata', 'receipt', 'missing_receipt'):
            with self.subTest(damage=damage):
                # Each damage is isolated in a backup clone of an intact ledger.
                if not self.store._read('SELECT * FROM claim_reviews'):
                    created = self.reviews.create(**self.request())
                else:
                    created = self.reviews.list()['items'][0]
                backup = self.root / ('backup-' + damage)
                clone_root = self.root / ('clone-' + damage)
                create_backup(self.store.root, backup); restore_backup(backup, clone_root)
                clone = Store(clone_root); service = ClaimReviews(clone)
                with db.transaction(clone.db_path) as connection:
                    if damage == 'payload':
                        connection.execute("UPDATE claim_reviews SET payload=payload||' ' WHERE id=?", (created['id'],))
                    elif damage == 'metadata':
                        connection.execute('UPDATE claim_reviews SET created_at=? WHERE id=?', ('2020-01-01T00:00:00+00:00', created['id']))
                    elif damage == 'receipt':
                        connection.execute('UPDATE claim_review_receipts SET request_digest=?', ('0' * 64,))
                    else:
                        connection.execute('DELETE FROM claim_review_receipts')
                self.assert_error(lambda: service.get(created['id']), 409)
                self.assert_error(lambda: service.create(**self.request()), 409)

    def test_original_claim_and_original_creation_receipt_tampering_are_detected(self):
        created = self.reviews.create(**self.request())
        backup = self.root / 'backup'; create_backup(self.store.root, backup)
        for damage in ('claim', 'claim_receipt', 'missing_claim_receipt', 'case', 'study'):
            clone_root = self.root / ('clone-' + damage); restore_backup(backup, clone_root)
            clone = Store(clone_root); service = ClaimReviews(clone)
            with db.transaction(clone.db_path) as connection:
                if damage == 'claim':
                    connection.execute("UPDATE research_claims SET payload=payload||' ' WHERE id=?", (self.batch['id'],))
                elif damage == 'claim_receipt':
                    connection.execute('UPDATE research_claim_receipts SET request_digest=?', ('0' * 64,))
                elif damage == 'missing_claim_receipt':
                    connection.execute('DELETE FROM research_claim_receipts')
                elif damage == 'case':
                    connection.execute("UPDATE research_cases SET payload=payload||' ' WHERE id=?", (self.case['id'],))
                else:
                    row = connection.execute('SELECT payload FROM author_studies WHERE id=?', (self.study['id'],)).fetchone()
                    payload = json.loads(row['payload']); payload['note'] = 'Changed original source meaning.'
                    connection.execute('UPDATE author_studies SET payload=? WHERE id=?', (json_text(payload), self.study['id']))
            for callback in (lambda: service.get(created['id']), lambda: service.export(created['id']),
                             lambda: service.create(**self.request()), lambda: service.status(self.batch['id'], 'count')):
                self.assert_error(callback, 409)

    def test_missing_replacement_parent_is_not_rebuilt(self):
        first = self.reviews.create(**self.request())
        request = self.request(key='replacement')
        request.update(supersedes_id=first['id'], expected_supersedes_digest=first['digest'])
        second = self.reviews.create(**request)
        connection = db.connect(self.store.db_path)
        try:
            # Simulate external SQLite corruption in this isolated fixture;
            # regular service writes preserve the declared foreign keys.
            connection.execute('PRAGMA foreign_keys=OFF')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM claim_review_receipts WHERE review_id=?', (first['id'],))
            connection.execute('DELETE FROM claim_reviews WHERE id=?', (first['id'],))
            connection.commit()
        finally:
            connection.close()
        self.assert_error(lambda: self.reviews.get(second['id']), 409)
        self.assert_error(lambda: self.reviews.create(**request), 409)

    def test_chain_and_ledger_size_bounds_fail_before_new_writes(self):
        with patch.object(claim_reviews, 'MAX_CHAIN', 3):
            request = self.request()
            for index in range(3):
                created = self.reviews.create(**request)
                request = self.request(key=f'next-{index}')
                request.update(supersedes_id=created['id'], expected_supersedes_digest=created['digest'])
            self.assert_error(lambda: self.reviews.create(**request), 413)
            self.assertEqual(self.reviews.list()['total'], 3)
        with patch.object(claim_reviews, 'MAX_RECORDS', 2):
            self.assert_error(lambda: self.reviews.list(), 413)
        with patch.object(claim_reviews, 'MAX_LEDGER_BYTES', 1):
            self.assert_error(lambda: self.reviews.get(created['id']), 413)

    def test_unreferenced_record_and_receipt_removed_together_need_external_checkpoint(self):
        # Honest boundary: local hashes cannot prove erased history when an
        # external writer also removes its only durable reference. An external
        # backup/checkpoint, not another local approval bit, is needed here.
        request = self.request()
        original = self.reviews.create(**request)
        checkpoint = {'review': self.store._read('SELECT * FROM claim_reviews'),
                      'receipts': self.store._read('SELECT * FROM claim_review_receipts')}
        with db.transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM claim_review_receipts')
            connection.execute('DELETE FROM claim_reviews')
        self.assert_error(lambda: self.reviews.get(original['id']), 404)
        self.assertEqual(self.reviews.list()['total'], 0)
        recreated = self.reviews.create(**request)
        self.assertEqual(recreated['digest'], original['digest'])
        self.assertNotEqual(checkpoint['review'], self.store._read('SELECT * FROM claim_reviews'))
        self.assertEqual(self.reviews.status(self.batch['id'], 'count')['human_records'], 0)

    def test_binary_payload_corruption_fails_closed(self):
        created = self.reviews.create(**self.request())
        with db.transaction(self.store.db_path) as connection:
            connection.execute('UPDATE claim_reviews SET payload=? WHERE id=?', (b'not-text', created['id']))
        self.assert_error(lambda: self.reviews.get(created['id']), 409)

    def test_bounded_pagination_and_exact_batch_filters(self):
        first = self.reviews.create(**self.request())
        self.reviews.create(**self.request(reviewer='Other synthetic fixture', key='another'))
        page = self.reviews.list(limit=1, claims_id=self.batch['id'], claim_id='count')
        self.assertEqual(page['total'], 2); self.assertEqual(len(page['items']), 1)
        self.assertEqual(self.reviews.list(offset=1000)['items'], [])
        self.assertEqual(self.reviews.list(source='human')['total'], 0)
        self.assertEqual(self.reviews.list(reviewer=first['reviewer'])['total'], 1)
        for kwargs in ({'limit': True}, {'limit': 101}, {'offset': -1}, {'source': 'model'},
                       {'reviewer': ' '}, {'claim_id': 'count'}, {'claims_id': '../private'}):
            self.assert_error(lambda: self.reviews.list(**kwargs), 422)

    def test_backup_preserves_exact_claim_case_and_review_history(self):
        first = self.reviews.create(**self.request())
        request = self.request(key='replacement')
        request.update(supersedes_id=first['id'], expected_supersedes_digest=first['digest'])
        second = self.reviews.create(**request)
        original_rows = {table: self.store._read(f'SELECT * FROM {table} ORDER BY rowid') for table in (
            'research_claims', 'research_claim_receipts', 'research_cases', 'research_case_receipts', 'claim_reviews', 'claim_review_receipts')}
        backup = self.root / 'backup'; create_backup(self.store.root, backup)
        restored_root = self.root / 'restored'; restore_backup(backup, restored_root)
        restored = Store(restored_root); service = ClaimReviews(restored)
        self.assertEqual(service.get(first['id']), first); self.assertEqual(service.get(second['id']), second)
        self.assertEqual(service.create(**request), second)
        for table, rows in original_rows.items():
            self.assertEqual(rows, restored._read(f'SELECT * FROM {table} ORDER BY rowid'))

    def test_final_actual_daily_source_fence_rolls_back_record_and_receipt(self):
        seed = self.store.seed_example()
        run = self.store.submit_run(seed['revision_id'], 'normalized_fixed', 'daily')
        job = self.store.claim('claim-review-test')
        run_task(job['task_path'], job['output_dir'], mode='normalized_fixed')
        self.store.finish(run['id'], 'claim-review-test', job['attempt_id'], 'completed')
        preview = self.cases.preview('daily_run', run['id'])
        case = self.cases.create('Verified synthetic daily evaluation', '', 'daily_run', run['id'], preview['source_digest'], 'daily-case')
        result = case['context']['results'][0]
        draft = deepcopy(self.draft); draft['metric_references'] = [{'case_id': case['id'], 'case_digest': case['digest'],
            'result_id': result['id'], 'result_digest': result['digest'], 'pointer': '/0/result/metrics/mean_rank_ic'}]
        batch = self.claims.create(case['id'], case['digest'], [draft], 'daily-claims')
        request = self.request(batch=batch)
        verified_run = self.store.get_run(run['id'])
        relative = next(relative for relative in verified_run['state']['candidates'][0]['artifacts'] if relative.endswith('/result.json'))
        artifact = Path(job['output_dir']) / relative
        original_bytes = artifact.read_bytes()
        record = self.reviews._record
        def changed_after_record(*args, **kwargs):
            result = record(*args, **kwargs)
            artifact.write_bytes(original_bytes + b' ')
            return result
        try:
            with patch.object(self.reviews, '_record', side_effect=changed_after_record):
                self.assert_error(lambda: self.reviews.create(**request), 409)
        finally:
            artifact.write_bytes(original_bytes)
        self.assertEqual(self.store._read('SELECT * FROM claim_reviews'), [])
        self.assertEqual(self.store._read('SELECT * FROM claim_review_receipts'), [])
        self.assertEqual(self.reviews.status(batch['id'], 'count')['human_records'], 0)


if __name__ == '__main__':
    unittest.main()
