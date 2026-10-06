"""Exercise the explicit target binding and strict JSON response over HTTP."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from paper_alpha.server.api import create_app
from paper_alpha.server import candidate_series
from paper_alpha.server.candidate_series_schema import CandidateSeriesResponse
from paper_alpha.workflow import run_task


class CandidateSeriesApiTests(unittest.TestCase):
    def test_bound_http_series_and_errors_do_not_write_review_records(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(Path(folder) / 'workspace')
            store = app.state.store
            example = store.seed_example()
            run = store.submit_run(example['revision_id'], 'normalized_fixed', 'series-http')
            claim = store.claim('series-http-worker')
            state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
            store.finish(run['id'], 'series-http-worker', claim['attempt_id'], state['status'])
            with TestClient(app) as client:
                detail = client.get('/api/runs/' + run['id']).json()
                target = next(item for item in detail['review_targets'] if item['candidate_id'] == 'alpha101')
                path = '/api/runs/' + run['id'] + '/candidates/alpha101/series'
                params = {key: target[key] for key in ('attempt_id', 'result_digest')}
                reply = client.get(path, params=params)
                self.assertEqual(reply.status_code, 200, reply.text)
                parsed = CandidateSeriesResponse.model_validate(reply.json())
                self.assertEqual(parsed.attempt_id, target['attempt_id'])
                self.assertEqual(parsed.points[-1].status, 'purged')
                self.assertIsNone(parsed.points[-1].coverage)
                self.assertEqual(client.get(path).status_code, 422)
                stale = client.get(path, params={**params, 'result_digest': 'f' * 64})
                self.assertEqual(stale.status_code, 409)
                self.assertIn('stale', stale.json()['detail'])
                with patch.object(candidate_series, 'MAX_RESPONSE_BYTES', 1):
                    self.assertEqual(client.get(path, params=params).status_code, 413)
                original = Path(claim['output_dir']) / parsed.source.artifact_name
                original.write_text('{}\n')
                self.assertEqual(client.get(path, params=params).status_code, 409)
                self.assertEqual(store._read('SELECT COUNT(*) AS count FROM reviews')[0]['count'], 0)
                self.assertEqual(store._read('SELECT COUNT(*) AS count FROM workflow_observations')[0]['count'], 0)
