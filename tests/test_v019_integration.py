"""Actual HTTP→services→worker contracts on isolated engineering fixtures."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from paper_alpha.server.api import create_app
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.storage import read_json

ROOT=Path(__file__).resolve().parents[1]

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.app=create_app(Path(temp.name).resolve()/'workspace');self.store=self.app.state.store
        self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def post(self,path,body,status=200):
        response=self.client.post(path,json=body);self.assertEqual(response.status_code,status,response.text);return response.json()

    def test_material_http_sources_and_durable_automation_annotation(self):
        materials=self.client.get('/api/semantic-materials').json();self.assertEqual(materials['total'],7)
        summary=self.client.get('/api/semantic-annotations/summary').json();self.assertEqual(summary['human_records'],0);self.assertIsNone(summary['semantic_quality_score'])
        material=materials['items'][0]
        self.assertEqual(self.client.get('/api/semantic-material-sources/alpha101_paper').headers['content-type'],'application/pdf')
        self.assertEqual(self.client.get('/api/semantic-material-sources/not-a-source').status_code,422)
        body={'material_sha256':material['material_sha256'],'case_id':material['case_id'],'source':'automation','reviewer':'HTTP synthetic parser fixture; no actual human judgment','confirmed_at':'2020-01-01T00:00:00Z',
              'dimensions':{key:{'outcome':'not_assessed','reason':'Synthetic HTTP test only; semantic truth is unknown.'} for key in ['evidence_accuracy','hypothesis_fidelity','mechanism_attribution','field_semantics','implementation_alignment']},'supersedes_id':None}
        preview=self.post('/api/semantic-annotations/preview',body);self.assertFalse(preview['original_result_approval']);self.assertFalse(preview['claim_approval'])
        bad=deepcopy(body);bad['claim_approval']=True;self.post('/api/semantic-annotations/preview',bad,422)
        body['idempotency_key']='http-fixture';saved=self.post('/api/semantic-annotations',body,201)
        self.assertEqual(saved,self.post('/api/semantic-annotations',body,201))
        changed=deepcopy(body);changed['dimensions']['evidence_accuracy']['reason']='Changed payload';self.post('/api/semantic-annotations',changed,409)
        self.assertEqual(self.client.get('/api/semantic-annotations/'+saved['id']+'/export').status_code,200)
        summary=self.client.get('/api/semantic-annotations/summary').json();self.assertEqual(summary['human_records'],0);self.assertEqual(summary['pending_cases'],7)
        self.assertEqual(self.store._read('SELECT * FROM reviews'),[]);self.assertEqual(self.store._read('SELECT * FROM research_claims'),[])

    def test_monthly_http_job_executes_original_worker_and_links_exact_case(self):
        preset=self.client.get('/api/research-protocols/presets').json()['presets'][1]
        protocol=self.post('/api/research-protocols',{'title':'HTTP bounded monthly fixture','note':'No market claim','config':preset['config']},201)
        config=self.client.get('/api/monthly-experiments/defaults').json()['config'];config['end_month']='2025-02'
        body={'source':{'kind':'monthly_fixture','protocol_id':protocol['id'],'protocol_digest':protocol['digest'],'config':config},'budget':{'max_steps':12,'max_failures':2,'max_seconds':180},'idempotency_key':'http-domain'}
        job=self.post('/api/domain-research-jobs',body,201);self.assertFalse(job['provider_connected'])
        self.assertEqual(self.store._read('SELECT * FROM monthly_experiments'),[])
        for action in ('validate','submit'):
            step=self.post('/api/domain-research-jobs/'+job['id']+'/advance',{'action':action,'idempotency_key':action});self.assertIsNone(step['error'])
        with worker_lock(self.store.root) as fd:self.assertTrue(Worker(self.store,fd).run_once())
        for action in ('observe','complete'):
            step=self.post('/api/domain-research-jobs/'+job['id']+'/advance',{'action':action,'idempotency_key':action});self.assertIsNone(step['error'])
        finished=self.client.get('/api/domain-research-jobs/'+job['id']).json();self.assertEqual(finished['state'],'completed')
        preview=self.client.get('/api/research-cases/preview',params={'source_kind':'monthly_experiment','source_id':finished['experiment_id']}).json()
        case=self.post('/api/research-cases',{'title':'Exact HTTP monthly result','note':'Automation fixture, not human approval','source_kind':'monthly_experiment','source_id':finished['experiment_id'],'source_digest':preview['source_digest'],'idempotency_key':'http-case'},201)
        self.assertEqual(case['context']['reviews'],[]);self.assertEqual(self.client.get('/api/domain-research-jobs/'+job['id']+'/markdown').status_code,200)

    def test_author_diagnostic_stops_without_monthly_queue(self):
        scan=read_json(ROOT/'evaluation_suites/v017/blocked_scan.json')
        study=self.post('/api/author-studies',{'title':'HTTP author diagnostic fixture','note':'Synthetic aggregate; no original MAT authentication','scan':scan,'parent_review_id':None,'author_panel_ids':[],'idempotency_key':'study'},201)
        job=self.post('/api/domain-research-jobs',{'source':{'kind':'author_study_diagnostic','study_id':study['id'],'study_digest':study['digest']},'budget':{'max_steps':4,'max_failures':1,'max_seconds':180},'idempotency_key':'author-domain'},201)
        step=self.post('/api/domain-research-jobs/'+job['id']+'/advance',{'action':'validate','idempotency_key':'check'})
        self.assertEqual(step['next_state'],'blocked');self.assertEqual(self.store._read('SELECT * FROM monthly_experiments'),[])

    def test_closed_domain_input_discriminator_rejects_mixed_or_arbitrary_sources(self):
        for source in [{'kind':'network','url':'https://example.invalid'},{'kind':'author_study_diagnostic','study_id':'author_study_'+'a'*64,'study_digest':'a'*64,'config':{} }]:
            self.post('/api/domain-research-jobs',{'source':source,'budget':{'max_steps':2,'max_failures':1,'max_seconds':30},'idempotency_key':'invalid'},422)
        self.assertEqual(self.store._read('SELECT * FROM domain_research_jobs'),[])

if __name__=='__main__':unittest.main()
