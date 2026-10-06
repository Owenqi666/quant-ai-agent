"""Isolated observation fixtures: no actual human measurements are produced."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.server.db import connect, transaction
from paper_alpha.server.service import REPO, TASK_KEYS, ServiceError, Store
from paper_alpha.server.workflow_observations import WorkflowObservations, _timing, _parse
from paper_alpha.server.workflow_observations_schema import PHASES
from paper_alpha.storage import digest, read_json
from paper_alpha.workflow import run_task


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def observation(service, *, source='automation', condition='manual_cli', minute=0, session='fixture-session'):
    value = service.template()['observation']
    start = START + timedelta(minutes=minute)
    value.update(record_status='observed', source=source, participant='Automated test fixture', session_id=session,
                 condition=condition, execution_order=1, code_commit='a' * 40,
                 environment={'machine': 'isolated-fixture', 'os': 'test', 'python': 'test', 'dependencies': 'test'},
                 allowed_tools=['same fixture tools'], prior_familiarity='Familiar synthetic fixture', practice_session=False,
                 predeclared_stop_condition='Stop at first terminal result', started_at=start.isoformat(),
                 finished_at=(start + timedelta(minutes=10)).isoformat(), completion='incomplete',
                 incomplete_reason='Automated fixture, not an actual human observation')
    value['bound_outputs']['external_run_reference'] = '/never/open/this/fixture-path'
    value['intervals'] = [{'phase': phase, 'kind': 'active',
                           'started_at': (start + timedelta(minutes=index)).isoformat(),
                           'ended_at': (start + timedelta(minutes=index, seconds=30)).isoformat(),
                           'status': 'ended', 'source_reference': None} for index, phase in enumerate(PHASES)]
    return value


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'workspace')
        self.service = WorkflowObservations(self.store)
        paper = self.store.add_paper((REPO / 'examples/alpha101/paper.pdf').read_bytes(), 'Fixture')
        task = read_json(REPO / 'evaluation_suites/v05/task.json')
        self.research = self.store.create_research('Fixture display title differs harmlessly', paper['id'],
            self.store.example_dataset_id, {key: task[key] for key in TASK_KEYS})
        self.research_id = self.research['id']
        self.revision_id = self.research['latest_revision_id']

    def tearDown(self):
        self.temp.cleanup()

    def workbench(self, *, source='automation', minute=20, complete=True):
        run = self.store.submit_run(self.revision_id, 'normalized_fixed', 'run-' + source)
        claim = self.store.claim('fixture-worker')
        state = run_task(claim['task_path'], claim['output_dir'], mode='normalized_fixed')
        self.store.finish(run['id'], 'fixture-worker', claim['attempt_id'], state['status'])
        run = self.store.get_run(run['id'])
        target = run['review_targets'][0]
        value = observation(self.service, source=source, condition='workbench', minute=minute)
        review_interval = value['intervals'][4]
        assessment = {'reviewer': value['participant'], 'expected_attempt_id': target['attempt_id'],
                      'expected_result_digest': target['result_digest'],
                      'dimensions': {key: {'outcome': 'passed', 'reason': 'Automated fixture judgment only.'} for key in (
                          'evidence_accuracy', 'hypothesis_fidelity', 'mechanism_attribution', 'field_semantics', 'implementation_alignment')},
                      'active_intervals': [{key: review_interval[key] for key in ('started_at', 'ended_at')}]}
        review = self.store.create_review(run['id'], 'alpha101', 'accepted', 'hypothesis', 'Automated fixture only', source,
                                         assessment, idempotency_key='review-' + source)
        value['dimensions'] = assessment['dimensions']
        value['bound_outputs'].update(revision_id=self.revision_id, run_id=run['id'], attempt_id=claim['attempt_id'],
                                     review_ids=[review['id']], external_run_reference=None,
                                     verification_evidence='Verified fixture engine artifacts', report_reference='Saved report.md fixture')
        review_interval['source_reference'] = review['id']
        if complete:
            value.update(completion='completed', incomplete_reason=None)
        return value, claim

    def test_template_and_strict_types_unknown_aggregate_nonfinite(self):
        with self.assertRaises(ServiceError):
            self.service.validate(self.research_id, self.service.template()['observation'])
        base = observation(self.service)
        for key, val in [('unexpected', 0), ('observed_active_seconds', 0), ('schema_version', True), ('execution_order', True),
                         ('source', 'agent'), ('participant', ' '), ('protocol_digest', '0' * 64),
                         ('started_at', '2024-01-01T00:00:00'), ('practice_session', 'false')]:
            value = deepcopy(base); value[key] = val
            with self.subTest(key=key), self.assertRaises(ServiceError):
                self.service.validate(self.research_id, value)
        value = deepcopy(base); value['environment']['extra'] = 'wrong'
        with self.assertRaises(ServiceError): self.service.validate(self.research_id, value)
        value = deepcopy(base); value['execution_order'] = float('nan')
        with self.assertRaises(ServiceError): self.service.validate(self.research_id, value)

    def test_known_partial_unmeasured_interrupted_and_no_wall_time(self):
        value = observation(self.service)
        result = self.service.validate(self.research_id, value)
        self.assertEqual(result['timing']['observed_active_seconds'], 180)
        self.assertEqual(result['timing']['full_active_seconds'], 180)
        self.assertIsNone(result['timing']['observed_waiting_seconds'])
        value['intervals'] = value['intervals'][:1]
        result = self.service.validate(self.research_id, value)
        self.assertEqual(result['timing']['observed_active_seconds'], 30)
        self.assertIsNone(result['timing']['full_active_seconds'])
        value['intervals'][0].update(status='interrupted', ended_at=None)
        result = self.service.validate(self.research_id, value)
        self.assertIsNone(result['timing']['observed_active_seconds'])
        self.assertEqual(result['timing']['interrupted_intervals'], 1)
        self.assertEqual(result['timing']['unmeasured_phases'], list(PHASES))
        value['finished_at'] = None
        # Historical interrupted observations keep an unknown finish, instead
        # of inventing an end or rejecting them as years of observed activity.
        result = self.service.validate(self.research_id, value)
        self.assertIsNone(result['timing']['observed_active_seconds'])

    def test_timestamp_overlap_future_and_interrupted_end_rejected(self):
        base = observation(self.service)
        invalid = []
        item = deepcopy(base); item['intervals'][1] = deepcopy(item['intervals'][0]); invalid.append(item)
        item = deepcopy(base); item['intervals'][1]['started_at'] = '2024-01-01T00:00:20Z'; invalid.append(item)
        item = deepcopy(base); item['intervals'][0]['status'] = 'interrupted'; invalid.append(item)
        item = deepcopy(base); item['intervals'][0]['ended_at'] = None; invalid.append(item)
        item = deepcopy(base); item['intervals'][0]['started_at'] = '2023-12-31T23:59:59Z'; invalid.append(item)
        item = deepcopy(base); item['finished_at'] = '2099-01-01T00:00:00Z'; invalid.append(item)
        item = deepcopy(base); item['intervals'][0]['ended_at'] = '2024-01-01T00:00:00Z'; invalid.append(item)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ServiceError): self.service.validate(self.research_id, value)

    def test_atomic_idempotency_identity_and_different_research_scope(self):
        value = observation(self.service)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.service.import_observation(self.research_id, value, 'same-request'), range(4)))
        self.assertEqual(len({result['id'] for result in results}), 1)
        self.assertEqual(self.service.list_observations(self.research_id)['total'], 1)
        changed = deepcopy(value); changed['limitations'] = ['Changed request']
        for payload, key in [(changed, 'same-request'), (value, 'another-key')]:
            with self.assertRaises(ServiceError) as caught: self.service.import_observation(self.research_id, payload, key)
            self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(ServiceError): self.service.get_observation('other-research', results[0]['id'])
        self.assertEqual(self.service.import_observation(self.research_id, value, 'same-request'), results[0])

    def test_cross_record_overlap_rejected_and_automation_not_human(self):
        value = observation(self.service)
        self.service.import_observation(self.research_id, value, 'first')
        next_value = deepcopy(value); next_value['session_id'] = 'second'
        with self.assertRaises(ServiceError): self.service.import_observation(self.research_id, next_value, 'second')
        summary = self.service.summary(self.research_id)
        self.assertEqual(summary['automation_records'], 1)
        self.assertEqual(summary['human_nonpractice_records'], 0)
        self.assertIsNone(summary['human_completion_rate'])
        self.assertEqual(summary['comparisons'], [])

    def test_external_reference_is_inert_and_inputs_are_declared(self):
        value = observation(self.service)
        value['bound_outputs']['external_run_reference'] = '/etc/passwd'
        with patch('builtins.open', side_effect=AssertionError('unexpected open')):
            # Path.read_text/read_bytes internally use io.open, so explicitly
            # prove the binding never passes this reference to the file loader.
            with patch('paper_alpha.server.workflow_observations.read_json', wraps=read_json) as reader:
                result = self.service.validate(self.research_id, value)
                self.assertFalse(any(str(call.args[0]) == '/etc/passwd' for call in reader.call_args_list))
        self.assertFalse(result['binding']['outputs_verified'])
        self.assertFalse(result['binding']['scientific_inputs_verified'])
        self.assertEqual(result['binding']['kind'], 'external_declared')

    def test_exact_workbench_binding_and_copied_review_once(self):
        value, claim = self.workbench()
        result = self.service.import_observation(self.research_id, value, 'bound')
        self.assertTrue(result['binding']['outputs_verified'])
        self.assertTrue(result['binding']['scientific_inputs_verified'])
        self.assertEqual(result['timing']['observed_active_seconds'], 180)
        self.assertEqual(result['timing']['phases'][4]['active_seconds'], 30)
        changed = deepcopy(value); changed['session_id'] = 'other'
        changed['intervals'][4]['ended_at'] = '2024-01-01T00:24:31+00:00'
        with self.assertRaises(ServiceError): self.service.validate(self.research_id, changed)
        exported = self.service.export_observation(self.research_id, result['id'])
        self.assertIn(result['payload_digest'], exported)
        # A committed observation is immutable history, not a fresh claim that
        # output files remain unchanged forever. Replay remains possible.
        (Path(claim['output_dir']) / 'report.md').write_text('tampered')
        self.assertEqual(self.service.import_observation(self.research_id, value, 'bound')['id'], result['id'])
        with self.assertRaises(ServiceError): self.service.validate(self.research_id, changed)

    def test_binding_rejects_wrong_research_attempt_source_and_changed_science(self):
        value, _ = self.workbench()
        invalid = []
        item = deepcopy(value); item['bound_outputs']['attempt_id'] = 'missing'; invalid.append(item)
        item = deepcopy(value); item['source'] = 'human'; invalid.append(item)
        item = deepcopy(value); item['participant'] = 'Someone else'; invalid.append(item)
        item = deepcopy(value); item['dimensions']['evidence_accuracy']['reason'] = 'Changed'; invalid.append(item)
        item = deepcopy(value); item['bound_outputs']['task_sha256'] = 'a' * 64; invalid.append(item)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ServiceError): self.service.validate(self.research_id, item)
        task = deepcopy(self.store.get_research(self.research_id)['revisions'][0]['task'])
        task['hypotheses'][0]['claim'] = 'A changed scientific statement'
        revision = self.store.create_revision(self.research_id, self.revision_id, task, 'Changed science')
        value.update(completion='incomplete', incomplete_reason='Fixture')
        value['bound_outputs'].update(revision_id=revision['id'], run_id=None, attempt_id=None, review_ids=[])
        value['intervals'][4]['source_reference'] = None
        with self.assertRaises(ServiceError): self.service.validate(self.research_id, value)

    def test_human_case_comparison_conditions_missingness_and_practice(self):
        manual = observation(self.service, source='human')
        workbench, _ = self.workbench(source='human', minute=20)
        manual.update(completion='completed', incomplete_reason=None, dimensions=deepcopy(workbench['dimensions']))
        manual['bound_outputs'].update(verification_evidence='Declared external fixture verification', report_reference='External fixture report')
        workbench['execution_order'] = 2
        first = self.service.import_observation(self.research_id, manual, 'manual-human-fixture')
        second = self.service.import_observation(self.research_id, workbench, 'workbench-human-fixture')
        summary = self.service.summary(self.research_id)
        self.assertEqual(summary['human_nonpractice_records'], 2)
        self.assertEqual(summary['human_completion_rate'], 1)
        self.assertEqual([x['records'] for x in summary['by_condition']], [1, 1])
        self.assertEqual([x['full_active_coverage_records'] for x in summary['by_condition']], [1, 1])
        self.assertTrue(summary['comparisons'][0]['comparable'])
        self.assertEqual(summary['comparisons'][0]['active_seconds_delta'], 0)
        self.assertEqual(set(summary['comparisons'][0]['observation_ids']), {first['id'], second['id']})
        practice = observation(self.service, source='human', minute=40, session='practice')
        practice['practice_session'] = True
        self.service.import_observation(self.research_id, practice, 'practice')
        changed = observation(self.service, source='human', minute=60, session='different-conditions')
        changed['environment']['machine'] = 'another machine'
        self.service.import_observation(self.research_id, changed, 'different-conditions')
        summary = self.service.summary(self.research_id)
        self.assertEqual(summary['practice_records'], 1)
        self.assertEqual(summary['human_nonpractice_records'], 3)
        self.assertEqual(sum(x['comparable'] for x in summary['comparisons']), 1)
        self.assertIsNone(next(x for x in summary['comparisons'] if not x['comparable'])['active_seconds_delta'])

    def test_summary_never_selects_faster_repeat_and_counts_incomplete(self):
        for index in range(2):
            value = observation(self.service, source='human', minute=index * 20, session=f'repeat-{index}')
            self.service.import_observation(self.research_id, value, f'repeat-{index}')
        summary = self.service.summary(self.research_id)
        self.assertEqual(summary['human_nonpractice_records'], 2)
        self.assertEqual(summary['human_completion_rate'], 0)
        self.assertFalse(summary['comparisons'][0]['comparable'])
        self.assertIsNone(summary['comparisons'][0]['active_seconds_delta'])

    def test_payload_and_identity_tampering_block_list_get_export_summary(self):
        value = observation(self.service)
        saved = self.service.import_observation(self.research_id, value, 'tamper')
        with transaction(self.store.db_path) as connection:
            connection.execute("UPDATE workflow_observations SET participant='changed' WHERE id=?", (saved['id'],))
        for action in [lambda: self.service.get_observation(self.research_id, saved['id']),
                       lambda: self.service.list_observations(self.research_id),
                       lambda: self.service.export_observation(self.research_id, saved['id']),
                       lambda: self.service.summary(self.research_id)]:
            with self.assertRaises(ServiceError) as caught: action()
            self.assertEqual(caught.exception.status, 409)

    def test_pagination_bounds_and_protocol_asset_drift(self):
        for limit, offset in [(0, 0), (101, 0), (True, 0), (20, -1), (20, 501)]:
            with self.assertRaises(ServiceError): self.service.list_observations(self.research_id, limit, offset)
        with patch('paper_alpha.server.workflow_observations.sha256', return_value='0' * 64):
            with self.assertRaises(ServiceError) as caught: self.service.validate(self.research_id, observation(self.service))
            self.assertEqual(caught.exception.status, 409)


if __name__ == '__main__':
    unittest.main()
