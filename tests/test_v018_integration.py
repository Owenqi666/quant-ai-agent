"""Public contract and worker fences on actual linked research jobs."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from paper_alpha.server.api import create_app
from paper_alpha.server.db import transaction
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.workflow import run_task


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.app = create_app(Path(temp.name) / 'home')
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.store = self.app.state.store
        self.seed = self.client.post('/api/examples/alpha101').json()
        self.research = self.store.get_research(self.seed['research_id'])

    def job(self):
        task = self.research['revisions'][0]['task']
        draft = {name: [deepcopy(row) for row in task[name] if row['id'] == identity]
                 for name, identity in [('evidence','alpha101-formula'),('hypotheses','h-alpha101'),('candidates','alpha101')]}
        response = self.client.post('/api/research-jobs', json={
            'research_id':self.research['id'], 'base_revision_id':self.seed['revision_id'],
            'draft':draft, 'budget':{'max_steps':10,'max_failures':2,'max_seconds':180},
            'idempotency_key':'contract-job'})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def step(self, job, action):
        response = self.client.post('/api/research-jobs/' + job['id'] + '/advance',
                                    json={'action':action,'idempotency_key':action})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(response.json()['error'],response.text)
        return response.json()

    def submitted(self):
        job = self.job()
        for action in ('validate','commit','submit'): self.step(job,action)
        return self.client.get('/api/research-jobs/' + job['id']).json()

    def worker(self):
        with worker_lock(self.store.root) as fd: Worker(self.store,fd).run_once()

    def expire(self, job):
        # Metadata integrity makes intentional database edits unsuitable here.
        # Advance the service clock after a genuine, fully persisted job submission.
        return patch('paper_alpha.server.research_jobs._wall_seconds', return_value=1000)

    def test_http_end_to_end_claims_and_guard(self):
        job = self.submitted(); self.worker()
        self.step(job,'observe'); self.step(job,'complete')
        run = self.store.get_run(job['run_id']); self.assertEqual(run['status'],'completed')
        preview = self.client.get('/api/research-cases/preview',params={'source_kind':'daily_run','source_id':run['id']}).json()
        case = self.client.post('/api/research-cases',json={'title':'Linked execution','note':'Automation fixture',
            'source_kind':'daily_run','source_id':run['id'],'source_digest':preview['source_digest'],'idempotency_key':'case'}).json()
        result = case['context']['results'][0]
        body = {'case_id':case['id'],'case_digest':case['digest'],'claims':[{'id':'metric','kind':'metric',
            'attribution':'project_convention','text':'This narrative remains unverified.','evidence_ids':[],
            'metric_references':[{'case_id':case['id'],'case_digest':case['digest'],'result_id':result['id'],
                'result_digest':result['digest'],'pointer':'/0/result/metrics/mean_rank_ic'}]}]}
        resolved = self.client.post('/api/research-claims/preview',json=body)
        self.assertEqual(resolved.status_code,200,resolved.text)
        self.assertEqual(resolved.json()['claims'][0]['semantic_fidelity'],'unverified')
        body['idempotency_key']='claim'
        saved = self.client.post('/api/research-claims',json=body)
        self.assertEqual(saved.status_code,201,saved.text)
        self.assertEqual(self.client.post('/api/research-claims',json=body).json(),saved.json())
        for resource,identity in [('research-jobs',job['id']),('research-claims',saved.json()['id'])]:
            self.assertEqual(self.client.get(f'/api/{resource}/{identity}/markdown').status_code,200)
        bad = deepcopy(body); bad['claims'][0]['metric_references'][0]['value']=999
        self.assertEqual(self.client.post('/api/research-claims',json=bad).status_code,422)
        guard = self.client.get('/api/researches/' + self.research['id'] + '/temporal-guard')
        self.assertEqual(guard.status_code,200,guard.text)
        self.assertEqual(self.client.get('/api/research-guards').status_code,200)
        task = deepcopy(self.research['revisions'][0]['task'])
        task['evaluation']['splits']['validation']['end']='2023-06-30'
        task['evaluation']['splits']['validation']['start']='2023-04-03'
        task['evaluation']['splits']['test']['start']='2023-07-03'
        conflict = self.client.post('/api/researches/' + self.research['id'] + '/revisions',json={
            'base_revision_id':self.store.get_research(self.research['id'])['latest_revision_id'],
            'task':task,'note':'Attempt to expose the protected test'})
        self.assertEqual(conflict.status_code,409,conflict.text)

    def test_expired_parent_prevents_engine_start_and_normal_run_still_works(self):
        job = self.submitted()
        with self.expire(job), patch('paper_alpha.server.runner.subprocess.Popen') as process:
            self.worker(); process.assert_not_called()
        run = self.store.get_run(job['run_id'])
        self.assertEqual(run['status'],'failed'); self.assertIn('budget exhausted',run['error'])
        ordinary = self.store.submit_run(self.seed['revision_id'],'normalized_fixed','ordinary')
        self.worker(); self.assertEqual(self.store.get_run(ordinary['id'])['status'],'completed')

    def test_expiry_during_publication_withholds_success_and_exports(self):
        job = self.submitted(); claim = self.store.claim('publication-worker')
        state = run_task(claim['task_path'],claim['output_dir'],mode='normalized_fixed')
        self.assertEqual(state['status'],'completed')
        with self.expire(job):
            self.store.finish(claim['id'],'publication-worker',claim['attempt_id'],'completed')
        result = self.store.get_run(claim['id'])
        self.assertEqual(result['status'],'failed'); self.assertEqual(self.store.list_artifacts(claim['id']),[])
        self.assertTrue(Path(claim['output_dir']).is_dir())

    def test_corrupt_budget_at_publication_is_saved_as_failure(self):
        job = self.submitted(); claim = self.store.claim('publication-worker')
        run_task(claim['task_path'],claim['output_dir'],mode='normalized_fixed')
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_jobs SET metadata_digest=? WHERE id=?',('0'*64,job['id']))
        self.store.finish(claim['id'],'publication-worker',claim['attempt_id'],'completed')
        result = self.store.get_run(claim['id'])
        self.assertEqual(result['status'],'failed'); self.assertIn('budget integrity failed',result['error'])
        self.assertEqual(self.store.list_artifacts(claim['id']),[])


if __name__ == '__main__': unittest.main()
