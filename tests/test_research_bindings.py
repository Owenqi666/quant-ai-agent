"""Exact source preparation tests; all count/panel inputs here are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from paper_alpha import eligibility, research_protocol
from paper_alpha.server.api import create_app
from paper_alpha.server.db import connect, transaction
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.server.author_panels import AuthorPanels
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.research_bindings import ResearchBindings
from paper_alpha.server.research_bindings_schema import BindingExport
from paper_alpha.storage import digest, json_text
from tests.test_author_studies import panel_input

ROOT = Path(__file__).resolve().parents[1]


class ResearchBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name).resolve() / 'home')
        self.service = ResearchBindings(self.store)
        self.paper = self.store.add_paper((ROOT / 'examples/alpha101/paper.pdf').read_bytes(), 'Fixed fixture document')
        self.protocol = ResearchProtocols(self.store).create('Fixture rules', '', research_protocol.presets()[1]['config'])
        scan = eligibility.demo_scan(); scan['plan']['minimum_assets'] = 50000; scan['plan_digest'] = digest(scan['plan'])
        self.panel = AuthorPanels(self.store).create('Synthetic row', '', panel_input(), 'panel')
        self.study = AuthorStudies(self.store).create('Synthetic screen', 'No original source scan performed.', scan, None, [self.panel['id']], 'study')
        self.preview = self.service.prepare(self.paper['id'], self.protocol['id'], self.study['id'], 'controlled_contract_fixture', 'Fixed contract')

    def create(self, **changes):
        return self.service.create(**(self.preview['request'] | {'preview_digest': self.preview['preview_digest'], 'idempotency_key': 'binding'} | changes))

    def rows(self, table):
        return self.store._read('SELECT * FROM ' + table)

    def test_prepare_derives_exact_refs_without_writes_or_execution(self):
        self.assertEqual(self.rows('research_bindings'), [])
        self.assertEqual(self.rows('domain_research_jobs'), [])
        self.assertEqual(self.rows('monthly_experiments'), [])
        self.assertEqual(self.preview['request']['panels'], [{'id': self.panel['id'], 'digest': self.panel['digest']}])
        context = self.preview['context']
        self.assertEqual(context['study']['scan_digest'], digest(self.study['scan']))
        self.assertEqual(context['window_rules']['momentum_history'], 'H-12..H-2')
        self.assertEqual(context['paper']['evidence_scope'], 'unrelated_fixture_document_contract')
        self.assertTrue(any(value.startswith('DATA_INSUFFICIENT:') for value in context['blockers']))
        self.assertFalse(context['execution_ready']); self.assertFalse(context['raw_source_reverified'])
        self.assertEqual(context['semantic_fidelity'], 'unverified')

    def test_content_address_replay_export_and_page(self):
        item = self.create()
        self.assertEqual(item, self.create())
        self.assertEqual(item, self.create(idempotency_key='another-key'))
        self.assertEqual(self.service.get(item['id']), item)
        self.assertEqual(self.service.list()['total'], 1)
        self.assertEqual(self.service.list(study_id=self.study['id'])['items'][0]['id'], item['id'])
        self.assertEqual(BindingExport.model_validate(self.service.export(item['id'])).binding_digest, item['digest'])
        self.assertIn(item['digest'], self.service.markdown(item['id']))
        self.assertEqual(len(self.rows('research_binding_receipts')), 2)

    def test_same_key_changed_note_conflicts_without_side_effect(self):
        self.create()
        preview = self.service.preview(**(self.preview['request'] | {'note': 'changed'}))
        with self.assertRaises(ServiceError) as raised:
            self.service.create(**preview['request'], preview_digest=preview['preview_digest'], idempotency_key='binding')
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(len(self.rows('research_bindings')), 1)

    def test_fixture_cannot_be_elevated_to_author_paper(self):
        with self.assertRaises(ServiceError):
            self.service.preview(**(self.preview['request'] | {'source_scope': 'author_paper'}))
        self.assertEqual(self.rows('research_bindings'), [])

    def test_every_digest_and_source_identity_is_exact(self):
        for field in ('paper_digest', 'protocol_digest', 'study_digest', 'source_sha256', 'plan_digest', 'scan_digest', 'result_digest'):
            with self.subTest(field=field), self.assertRaises(ServiceError):
                self.service.preview(**(self.preview['request'] | {field: '0' * 64}))
        for change in ({'source_filename': 'USData.mat'}, {'panels': []}, {'panels': [{'id': self.panel['id'], 'digest': '0' * 64}]}):
            with self.subTest(change=change), self.assertRaises(ServiceError):
                self.service.preview(**(self.preview['request'] | change))

    def test_protocol_different_window_is_incompatible(self):
        config = deepcopy(self.protocol['config']); config['mom_window_months'] = 10
        protocol = ResearchProtocols(self.store).create('Ten months', '', config)
        with self.assertRaises(ServiceError):
            self.service.prepare(self.paper['id'], protocol['id'], self.study['id'], 'controlled_contract_fixture', 'Invalid')

    def test_passing_counts_still_do_not_grant_execution(self):
        scan = eligibility.demo_scan()
        study = AuthorStudies(self.store).create('Enough synthetic counts', '', scan, None, [], 'passed-study')
        preview = self.service.prepare(self.paper['id'], self.protocol['id'], study['id'], 'controlled_contract_fixture', 'Passed counts')
        self.assertEqual(preview['context']['study']['summary']['status'], 'screen_passed')
        self.assertFalse(preview['context']['execution_ready'])
        self.assertTrue(any(value.startswith('AUTHOR_PORTFOLIO_METHOD_UNEXECUTED:') for value in preview['context']['blockers']))

    def test_closed_http_no_raw_source_authority_or_path(self):
        client = TestClient(create_app(self.store.root))
        base = self.preview['request'] | {'preview_digest': self.preview['preview_digest'], 'idempotency_key': 'http'}
        for extra in ({'raw_source_reverified': True}, {'source_path': '/tmp/fake.mat'}, {'url': 'https://example.org'}, {'execution_ready': True}):
            with self.subTest(extra=extra):
                self.assertEqual(client.post('/api/research-bindings', json=base | extra).status_code, 422)
        response = client.post('/api/research-bindings/prepare', json={key: base[key] for key in ('paper_id','protocol_id','study_id','source_scope','title','note')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.preview)

    def test_concurrent_exact_creation_writes_one_body(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: self.create(), range(2)))
        self.assertEqual(values[0], values[1])
        self.assertEqual(len(self.rows('research_bindings')), 1)
        self.assertEqual(len(self.rows('research_binding_receipts')), 1)

    def test_frozen_source_tamper_rejected_by_get_list_and_replay(self):
        item = self.create()
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE author_studies SET digest=? WHERE id=?', ('0' * 64, self.study['id']))
        for operation in (lambda: self.service.get(item['id']), self.service.list, self.create):
            with self.assertRaises(ServiceError): operation()

    def test_actual_pdf_bytes_are_rechecked(self):
        item = self.create(); paper = self.store._fetch('papers', self.paper['id'])
        Path(paper['pdf_path']).write_bytes(b'changed PDF')
        with self.assertRaises(ServiceError): self.service.get(item['id'])
        with self.assertRaises(ServiceError): self.create()

    def test_original_panel_tamper_rejected(self):
        item = self.create()
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE author_panels SET digest=? WHERE id=?', ('0' * 64, self.panel['id']))
        with self.assertRaises(ServiceError): self.service.get(item['id'])

    def test_record_and_receipt_metadata_tamper_rejected(self):
        item = self.create()
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_binding_receipts SET created_at=?', ('altered',))
        with self.assertRaises(ServiceError): self.create()
        with self.assertRaises(ServiceError): self.service.get(item['id'])
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_bindings SET created_at=?', ('altered',))
        with self.assertRaises(ServiceError): self.service.get(item['id'])

    def test_missing_receipt_is_not_rebuilt_by_replay_or_get(self):
        item = self.create()
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM research_binding_receipts')
        for operation in (self.create, self.service.list, lambda: self.service.get(item['id']),
                          lambda: self.service.preview(**self.preview['request'])):
            with self.assertRaises(ServiceError): operation()
        self.assertEqual(self.rows('research_binding_receipts'), [])

    def test_rehashed_receipt_cannot_point_to_another_request(self):
        item = self.create(); row = self.rows('research_binding_receipts')[0]
        row['request_digest'] = '0' * 64
        row['receipt_digest'] = digest({k: v for k, v in row.items() if k != 'receipt_digest'})
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_binding_receipts SET request_digest=?,receipt_digest=?',
                               (row['request_digest'], row['receipt_digest']))
        with self.assertRaises(ServiceError): self.service.get(item['id'])
        with self.assertRaises(ServiceError): self.create()

    def test_after_final_projection_pdf_tamper_rolls_back_all_publication(self):
        original = self.service.preview; calls = 0
        paper = self.store._fetch('papers', self.paper['id'])
        def changed(**request):
            nonlocal calls
            calls += 1
            value = original(**request)
            if calls == 2:
                Path(paper['pdf_path']).write_bytes(b'changed after projection')
            return value
        with patch.object(self.service, 'preview', side_effect=changed), self.assertRaises(ServiceError):
            self.create()
        self.assertEqual(self.rows('research_bindings'), [])
        self.assertEqual(self.rows('research_binding_receipts'), [])

    def test_backup_restore_preserves_binding_and_original_receipt(self):
        from paper_alpha.server.backup import create_backup, restore_backup
        item = self.create(); root = Path(self.temp.name).resolve()
        create_backup(self.store.root, root / 'backup')
        restore_backup(root / 'backup', root / 'restored')
        service = ResearchBindings(Store(root / 'restored'))
        self.assertEqual(service.get(item['id']), item)
        self.assertEqual(service.create(**self.preview['request'], preview_digest=self.preview['preview_digest'], idempotency_key='binding'), item)

    def test_final_fence_prevents_publish_on_changed_projection(self):
        original = self.service.preview; calls = 0
        def changed(**request):
            nonlocal calls
            calls += 1
            value = original(**request)
            if calls == 2:
                value['context']['limitations'].append('changed at final fence')
            return value
        with patch.object(self.service, 'preview', side_effect=changed), self.assertRaises(ServiceError):
            self.create()
        self.assertEqual(self.rows('research_bindings'), [])
        self.assertEqual(self.rows('research_binding_receipts'), [])

    def test_noncanonical_or_oversized_record_fails_closed(self):
        item = self.create()
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_bindings SET payload=?', ('[]',))
        with self.assertRaises(ServiceError): self.service.get(item['id'])
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_bindings SET payload=?', ('x' * (2 * 1024 * 1024 + 1),))
        with self.assertRaises(ServiceError): self.service.get(item['id'])


if __name__ == '__main__':
    unittest.main()
