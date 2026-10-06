"""The guided context endpoint is read-only and strictly typed over HTTP."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from paper_alpha.server.api import create_app
from paper_alpha.server.service import REPO, TASK_KEYS
from paper_alpha.storage import read_json


class ObservationContextApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name) / 'workspace')
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_context_does_not_create_observations_or_hide_protocol_mismatch(self):
        sample = self.client.post('/api/examples/alpha101', json={}).json()
        research = self.client.get('/api/researches/' + sample['research_id']).json()
        path = '/api/researches/' + research['id']
        reply = self.client.get(path + '/workflow-observation-context')
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertFalse(reply.json()['compatibility']['compatible'])
        self.assertIsNone(reply.json()['selected'])
        self.assertEqual(self.client.get(path + '/workflow-observations').json()['total'], 0)
        task = read_json(REPO / 'evaluation_suites/v05/task.json')
        created = self.client.post('/api/researches', json={'title': 'Guided HTTP fixture',
            'paper_id': research['paper_id'], 'dataset_id': research['dataset_id'],
            'task': {key: task[key] for key in TASK_KEYS}}).json()
        path = '/api/researches/' + created['id']
        reply = self.client.get(path + '/workflow-observation-context', params={'revision_id': created['latest_revision_id']})
        self.assertEqual(reply.status_code, 200, reply.text)
        context = reply.json()
        self.assertTrue(context['compatibility']['compatible'])
        self.assertEqual(context['revision_id'], created['latest_revision_id'])
        self.assertEqual(context['runs'], [])
        self.assertIsNone(context['selected'])
        self.assertEqual(context['runtime']['version'], '0.22.0')
        self.assertEqual(self.client.get(path + '/workflow-observations').json()['total'], 0)
        self.assertEqual(self.client.get(path + '/workflow-observation-context', params={'attempt_id': 'unbound'}).status_code, 422)
        self.assertEqual(self.client.get(path + '/workflow-observation-context', params={'run_id': 'missing'}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
