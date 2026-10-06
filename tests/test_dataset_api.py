"""Dataset HTTP boundaries and imported data survive portable recovery."""
from copy import deepcopy
from pathlib import Path
import unittest

from tests import test_server
from paper_alpha.server.api import create_app
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.dataset_imports import DatasetImports
from paper_alpha.server.service import Store
from paper_alpha.storage import read_json


FIXTURE = Path(__file__).parent / 'fixtures/datasets'


class DatasetAPI(unittest.TestCase):
    setUp = test_server.ServerCase.setUp
    tearDown = test_server.ServerCase.tearDown
    execute = test_server.ServerCase.execute

    def upload(self, key='upload', metadata=None):
        response = self.client.post('/api/dataset-imports', data={'title':'Independent synthetic data', 'idempotency_key':key}, files={
            'file':('market.csv', (FIXTURE / 'market.csv').read_bytes(), 'text/csv'),
            'metadata':('metadata.json', metadata if metadata is not None else (FIXTURE / 'metadata.json').read_bytes(), 'application/json')})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_import_new_research_and_portable_restore(self):
        old = self.store.get_research(self.example['research_id'])
        receipt = self.upload()
        self.assertEqual(self.upload()['id'], receipt['id'])
        validated = self.client.post('/api/dataset-imports/' + receipt['id'] + '/validate', json={'idempotency_key':'validate'}).json()
        self.assertEqual(validated['status'], 'valid')
        payload = {'idempotency_key':'register', 'input_digest':validated['input_digest'],
                   'validation_attempt_id':validated['latest_validation']['id'], 'report_digest':validated['latest_validation']['report_digest']}
        result = self.client.post('/api/dataset-imports/' + receipt['id'] + '/register', json=payload)
        self.assertEqual(result.status_code, 200, result.text)
        dataset_id = result.json()['registered_dataset_id']
        self.assertNotEqual(dataset_id, self.store.example_dataset_id)
        detail = self.client.get('/api/datasets/' + dataset_id).json()
        self.assertEqual(detail['registration']['kind'], 'validated_import')
        self.assertNotIn(str(self.root), str(detail))
        self.assertEqual(self.client.get('/api/datasets/' + self.store.example_dataset_id).json()['registration']['kind'], 'legacy_registration')
        self.assertEqual(self.store.seed_example(), self.example)
        self.assertEqual(self.store.get_research(self.example['research_id']), old)
        task = deepcopy(old['revisions'][0]['task'])
        request = {'title':'Independent calendar study', 'paper_id':old['paper_id'], 'dataset_id':dataset_id, 'task':task}
        self.assertEqual(self.client.post('/api/researches', json=request).status_code, 422)
        task['evaluation'] = read_json(FIXTURE / 'research_config.json')
        task['evaluation']['min_assets'] = 8
        self.assertEqual(self.client.post('/api/researches', json=request).status_code, 422)
        task['evaluation']['min_assets'] = 5
        created = self.client.post('/api/researches', json=request)
        self.assertEqual(created.status_code, 201, created.text)
        research = created.json()
        self.assertEqual(research['preflight']['candidates'][2]['code'], 'required_field_missing')
        run, _ = self.execute('second-data', 'normalized_fixed', research['latest_revision_id'])
        self.assertTrue(run['verification']['verified'])
        review = self.store.create_review(run['id'], 'alpha006', 'accepted', 'implementation', 'Automated integration', 'automation')
        case = self.store.approve_case(review['id'], 'evaluated', 'Same registered dataset')
        self.assertTrue(self.store.run_regression_check(run['id'], [case['id']])['passed'])
        old_run, _ = self.execute('old-data', 'normalized_fixed')
        mismatch = self.store.run_regression_check(old_run['id'], [case['id']])
        self.assertEqual(mismatch['outcome'], 'not_comparable')
        self.assertIn('dataset_id', mismatch['results'][0]['differences'])
        # Include an unregistered valid receipt, then register it after restore.
        pending = self.upload('pending')
        pending = self.client.post('/api/dataset-imports/' + pending['id'] + '/validate', json={'idempotency_key':'pending-validation'}).json()
        backup, restored = self.root.resolve().parent / (self.root.name + '-backup'), self.root.resolve().parent / (self.root.name + '-restored')
        try:
            create_backup(self.root.resolve(), backup)
            restore_backup(backup, restored)
            store = Store(restored)
            self.assertTrue(store.get_run(run['id'])['verification']['verified'])
            imports = DatasetImports(restored, store.db_path)
            self.assertEqual(imports.get(receipt['id'])['registered_dataset_id'], dataset_id)
            registered = imports.register(pending['id'], pending['input_digest'], pending['latest_validation']['id'], pending['latest_validation']['report_digest'], 'restored-register')
            self.assertEqual(registered['registered_dataset_id'], dataset_id)
        finally:
            import shutil
            shutil.rmtree(backup, ignore_errors=True)
            shutil.rmtree(restored, ignore_errors=True)

    def test_invalid_metadata_kept_and_chunked_size_checked(self):
        receipt = self.upload(metadata=b'{"duplicate":1,"duplicate":2}')
        report = self.client.post('/api/dataset-imports/' + receipt['id'] + '/validate', json={'idempotency_key':'bad-validation'}).json()
        self.assertEqual(report['status'], 'invalid')
        self.assertEqual(len(self.store.list_datasets()), 1)
        self.assertEqual(self.client.get('/api/dataset-imports/' + receipt['id']).json()['latest_validation']['status'], 'invalid')
        # Iterator body has no Content-Length; the ASGI boundary still counts bytes.
        response = self.client.post('/api/dataset-imports', content=iter([b'x' * (1024 * 1024)] * 19),
                                    headers={'Content-Type':'multipart/form-data; boundary=bounded'})
        self.assertEqual(response.status_code, 413)
        response = self.client.post('/api/dataset-imports', data={'title':'bad','idempotency_key':'bad'}, files={'file':('x.csv',b'x')})
        self.assertEqual(response.status_code, 422)

    def test_preflight_preserves_spaced_expression_and_reports_warmup(self):
        original = self.store.get_research(self.example['research_id'])
        task = deepcopy(original['revisions'][0]['task'])
        task['candidates'][0]['expression'] = '  close  '
        research = self.store.create_research('spaced', original['paper_id'], original['dataset_id'], task)
        self.assertEqual(research['revisions'][0]['task']['candidates'][0]['expression'], '  close  ')
        self.assertEqual(research['preflight']['candidates'][0]['status'], 'ready')
        task['candidates'][0]['expression'] = 'delay(delay(close, 252), 252)'
        research = self.store.create_research('long warmup', original['paper_id'], original['dataset_id'], task)
        self.assertEqual(research['preflight']['candidates'][0]['code'], 'no_validation_rows_after_warmup')
        self.assertEqual(research['preflight']['candidates'][0]['warmup_rows'], 504)


if __name__ == '__main__':
    unittest.main()
