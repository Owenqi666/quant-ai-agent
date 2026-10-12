"""Formula-only HTTP restore fixtures; scientific unknowns stay explicit."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from paper_alpha.server.api import create_app
from paper_alpha.server.evidence_lineage_schema import LineageHypothesis
from paper_alpha.server.response_schemas import (
    HypothesisResponse, FormulaEngineeringHypothesisResponse, ResearchTask,
)
from paper_alpha.server.research_jobs_schema import ENGINEERING_ONLY_ASSUMPTION
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.storage import digest, json_text


class FormulaEngineeringResponseContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / 'workspace')
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.store = self.app.state.store; self.seed = self.store.seed_example()
        research = self.store.get_research(self.seed['research_id'])
        self.base = research['revisions'][0]['task']
        self.hypothesis = deepcopy(self.base['hypotheses'][0])
        self.hypothesis.update(economic_mechanism=None, mechanism_attribution='unresolved')
        self.hypothesis['assumptions'].append(ENGINEERING_ONLY_ASSUMPTION)

    def test_legacy_hypothesis_schema_and_nonnull_dump_unchanged(self):
        self.assertEqual(digest(HypothesisResponse.model_json_schema()),
                         '3d3c0bf31e794e8ddf18c654aeb81482c46a0a97a5b1e396d4ac59ffd1e00e00')
        ordinary = self.base['hypotheses'][0]
        self.assertEqual(json_text(HypothesisResponse.model_validate(ordinary).model_dump()), json_text(ordinary))
        parsed = ResearchTask.model_validate(self.base)
        self.assertIsInstance(parsed.hypotheses[0], HypothesisResponse)
        self.assertNotIsInstance(parsed.hypotheses[0], FormulaEngineeringHypothesisResponse)
        self.assertEqual(json_text(parsed.model_dump(exclude_unset=True)), json_text(self.base))

    def test_response_and_lineage_null_branches_require_exact_engineering_marker(self):
        parsed = FormulaEngineeringHypothesisResponse.model_validate(self.hypothesis)
        self.assertEqual(parsed.model_dump(), self.hypothesis)
        final = {'id': self.hypothesis['id'], 'status': 'unknown', 'inheritance_intact': False,
                 'original': None, 'adopted': None, 'final': self.hypothesis, 'changes': []}
        self.assertEqual(LineageHypothesis.model_validate(final).model_dump(), final)
        for edit in [lambda h: h['assumptions'].remove(ENGINEERING_ONLY_ASSUMPTION),
                     lambda h: h['assumptions'].__setitem__(-1, ENGINEERING_ONLY_ASSUMPTION + ' '),
                     lambda h: h.update(mechanism_attribution='paper_original'),
                     lambda h: h.update(economic_mechanism='A fabricated resolved mechanism')]:
            bad = deepcopy(self.hypothesis); edit(bad)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                ResearchTask.model_validate({**self.base, 'hypotheses': [bad]})
            with self.subTest(lineage_edit=edit), self.assertRaises(ValueError):
                LineageHypothesis.model_validate({**final, 'final': bad})

    def test_actual_research_revision_queued_completed_run_and_unknown_lineage_restore(self):
        task = deepcopy(self.base)
        task['hypotheses'] = [self.hypothesis]
        task['candidates'] = [c for c in task['candidates'] if c['hypothesis_id'] == self.hypothesis['id']][:1]
        source = self.store.get_research(self.seed['research_id'])
        request = {'title': 'Explicit formula engineering HTTP restore fixture',
                   'paper_id': source['paper_id'], 'dataset_id': source['dataset_id'],
                   'task': task, 'idempotency_key': 'fixture-research'}
        created = self.client.post('/api/researches', json=request)
        self.assertEqual(created.status_code, 201, created.text)
        created = created.json(); rid = created['id']; revision = created['revisions'][0]
        # Repeat GET represents browser reload/safe recovery of persisted IDs.
        for _ in range(2):
            recovered = self.client.get('/api/researches/' + rid)
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json(), created)
        # There is no separate revision GET route. Its actual POST response and
        # inclusion in research GET are both covered without adding a route.
        changed = self.client.post('/api/researches/' + rid + '/revisions', json={
            'base_revision_id': revision['id'], 'task': task,
            'note': 'Fixture readback only; no review verdict', 'idempotency_key': 'fixture-revision'})
        self.assertEqual(changed.status_code, 201, changed.text); revision = changed.json()
        self.assertIsNone(revision['task']['hypotheses'][0]['economic_mechanism'])
        self.assertEqual(revision['task']['hypotheses'][0]['mechanism_attribution'], 'unresolved')
        recovered = self.client.get('/api/researches/' + rid)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()['revisions'][-1], revision)
        submitted = self.client.post('/api/runs', json={'revision_id': revision['id'],
            'mode': 'normalized_fixed', 'idempotency_key': 'fixture-run'})
        self.assertEqual(submitted.status_code, 201, submitted.text); run_id = submitted.json()['id']
        queued = self.client.get('/api/runs/' + run_id)
        self.assertEqual(queued.status_code, 200, queued.text)
        self.assertEqual(queued.json()['status'], 'queued')
        with worker_lock(self.store.root) as descriptor:
            self.assertTrue(Worker(self.store, descriptor).run_once())
        completed = self.client.get('/api/runs/' + run_id)
        self.assertEqual(completed.status_code, 200, completed.text); run = completed.json()
        self.assertEqual(run['status'], 'completed'); self.assertEqual(run['reviews'], [])
        self.assertTrue(run['verification']['verified'])
        self.assertIsNone(run['state']['task']['hypotheses'][0]['economic_mechanism'])
        self.assertEqual(self.client.get('/api/runs/' + run_id).json(), run)
        target = run['review_targets'][0]
        lineage = self.client.get('/api/runs/' + run_id + '/evidence-lineage', params={
            'candidate_id': target['candidate_id'], 'expected_attempt_id': target['attempt_id'],
            'expected_result_digest': target['result_digest'],
            'expected_state_digest': run['regression_target']['result_digest']})
        self.assertEqual(lineage.status_code, 200, lineage.text)
        self.assertEqual(lineage.json()['status'], 'unknown')
        self.assertEqual(lineage.json()['reason_code'], 'MATERIAL_LINEAGE_NOT_RECORDED')
        self.assertIsNone(lineage.json()['hypothesis']['final']['economic_mechanism'])
        self.assertFalse(lineage.json()['llm_api_called'])
        self.assertEqual(self.store._read('SELECT id FROM reviews'), [])


if __name__ == '__main__':
    unittest.main()
