"""Real isolated tool fixtures; no human label or original-workspace write."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.server.claim_reviews import ClaimReviews
from paper_alpha.server.observation_context import ObservationContextService
from paper_alpha.server.research_cases import CASE_FIELDS, ResearchCases
from paper_alpha.server.research_claims import FIELDS as CLAIM_FIELDS, ResearchClaims
from paper_alpha.server.semantic_annotations import SemanticAnnotations
from paper_alpha.server.service import REPO, TASK_KEYS, Store
from paper_alpha.storage import digest, json_text, read_json
from paper_alpha.workflow import run_task
from scripts import human_review_packet as packet


def build_fixture(root, *, single=True):
    """Reusable B HTTP fixture with actual engine/Case/Claim/Target objects.

    root must be a fresh caller-owned test directory, never the live workspace.
    No review, observation or material label is added. The returned runtime has
    the real current checkout declaration, which may lack a portable commit.
    """
    root = Path(root).resolve()
    store = Store(root / 'workspace')
    paper = store.add_paper((REPO / 'examples/alpha101/paper.pdf').read_bytes(), 'Isolated review preparation fixture')
    task_path = REPO / ('evaluation_suites/v05/task.json' if single else 'examples/alpha101/task.json')
    raw = read_json(task_path)
    task = {key: raw[key] for key in TASK_KEYS}
    research = store.create_research('Isolated synthetic Alpha101 fixture', paper['id'], store.example_dataset_id, task)
    run = store.submit_run(research['latest_revision_id'], 'normalized_fixed', 'fixture-run')
    job = store.claim('packet-fixture-worker')
    state = run_task(job['task_path'], job['output_dir'], mode='normalized_fixed')
    store.finish(run['id'], 'packet-fixture-worker', job['attempt_id'], state['status'])
    cases = ResearchCases(store)
    preview = cases.preview('daily_run', run['id'])
    case = cases.create('Isolated synthetic validation review case', '', 'daily_run', run['id'], preview['source_digest'], 'fixture-case')
    material = SemanticAnnotations(store).material('alpha101_formula')
    request = packet.build_claim_request(case, material)
    claims = ResearchClaims(store).create(**request, idempotency_key='fixture-claims')
    targets = [ClaimReviews(store).target(claims['id'], item['id']) for item in request['claims']]
    context_service = ObservationContextService(store)
    runtime = (context_service.get_context(research['id'], run_id=run['id'], attempt_id=job['attempt_id'])
               if single else context_service.get_context(research['id']))
    health = {'status': 'ok', 'version': runtime['runtime']['version'],
              'database_schema': int(store._read("SELECT value FROM settings WHERE key='schema_version'")[0]['value']),
              'ai_enabled': False,
              'workspace_id': hashlib.sha256(str(store.root.resolve()).encode()).hexdigest(), 'worker': store.worker_health()}
    return {'store': store, 'case': case, 'material': material, 'request': request, 'claims': claims,
            'targets': targets, 'runtime': runtime, 'health': health, 'research': research, 'run': run, 'job': job}


def rebind_case(case):
    """Rehash mutated snapshots to exercise semantic linkage, not hash-only checks."""
    case['source_digest'] = digest({key: case[key] for key in ('source_kind', 'source_id', 'context')})
    case['digest'] = digest({key: case[key] for key in CASE_FIELDS})
    case['id'] = 'research_case_' + case['digest']
    return case


def replace_candidates(case, candidates):
    result = case['context']['results'][0]
    result['payload_json'] = json_text(candidates)
    result['digest'] = digest(candidates)
    return rebind_case(case)


def rehash_packet(value):
    value['packet_digest'] = digest({key: item for key, item in value.items() if key != 'packet_digest'})
    return value


class HumanReviewPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.fixture = build_fixture(Path(cls.temp.name) / 'single')
        cls.multi = build_fixture(Path(cls.temp.name) / 'multiple', single=False)
        cls.snapshot = packet.build_packet(**{key: cls.fixture[key] for key in ('case', 'material', 'claims', 'targets', 'runtime', 'health')})

    def assert_bad_case(self, case, material=None):
        with self.assertRaises(ValueError):
            packet.build_claim_request(case, material or self.fixture['material'])

    def assert_bad_packet(self, value):
        with self.assertRaises(ValueError):
            packet.verify_packet(rehash_packet(value))

    def test_real_result_packet_is_exact_and_unjudged(self):
        verified = packet.verify_packet(self.snapshot)
        self.assertTrue(verified['passed'])
        self.assertEqual(verified['scope'], 'snapshot_integrity_only_not_semantic_quality')
        self.assertFalse(verified['pdf_bytes_reverified'])
        self.assertFalse(verified['runtime_authenticity_verified'])
        self.assertIsNone(self.snapshot['human_judgments'])
        self.assertIsNone(self.snapshot['semantic_quality_score'])
        self.assertFalse(self.snapshot['llm_api_called'])
        self.assertNotIn('idempotency_key', self.snapshot['claim_request'])
        self.assertEqual(len(self.snapshot['claims']['claims']), 3)
        for claim in self.snapshot['claims']['claims']:
            self.assertEqual((claim['actor'], claim['status'], claim['semantic_fidelity']), ('automation', 'draft', 'unverified'))
        self.assertEqual(self.fixture['store']._read('SELECT * FROM claim_reviews'), [])
        self.assertEqual(self.fixture['store']._read('SELECT * FROM semantic_annotations'), [])
        self.assertEqual(self.fixture['store']._read('SELECT * FROM workflow_observations'), [])

    def test_metric_values_come_from_exact_actual_alpha101_result(self):
        candidates = read_json(Path(self.fixture['job']['output_dir']) / 'state.json')['candidates']
        # Read the canonical frozen context as well: state and Case share the tool values.
        original = next(item for item in candidates if item['id'] == 'alpha101')['result']['metrics']
        metric = self.snapshot['claims']['claims'][1]
        self.assertEqual([item['value'] for item in metric['metrics']], [original[name] for name in packet.METRICS])
        self.assertTrue(all(item['verification'] == 'verified_result_value' for item in metric['metrics']))
        self.assertNotIn(str(original['mean_rank_ic']), metric['narrative_text'])
        self.assertIn(str(original['mean_rank_ic']), metric['authoritative_display'])

    def test_alpha101_not_first_is_located_by_actual_candidate_identity(self):
        request = self.multi['request']
        refs = request['claims'][1]['metric_references']
        self.assertTrue(all(ref['pointer'].startswith('/1/result/metrics/') for ref in refs))
        candidates = packet._json(self.multi['case']['context']['results'][0]['payload_json'])
        self.assertEqual(candidates[1]['id'], 'alpha101')
        self.assertNotEqual(candidates[0]['id'], 'alpha101')
        self.assertEqual(self.multi['claims']['claims'][1]['metrics'][0]['value'], candidates[1]['result']['metrics']['mean_rank_ic'])
        # Multiple candidates do not satisfy the fixed human protocol for packet export.
        with self.assertRaises(ValueError):
            packet.build_packet(**{key: self.multi[key] for key in ('case', 'material', 'claims', 'targets', 'runtime', 'health')})

    def test_candidate_id_alias_supported_but_conflicting_or_wrong_identity_rejected(self):
        case = deepcopy(self.fixture['case'])
        candidates = packet._json(case['context']['results'][0]['payload_json'])
        candidates[0]['candidate_id'] = candidates[0].pop('id')
        request = packet.build_claim_request(replace_candidates(case, candidates), self.fixture['material'])
        self.assertTrue(request['claims'][1]['metric_references'][0]['pointer'].startswith('/0/'))
        for identity in ({'id': 'wrong-alpha'}, {'id': 'alpha101', 'candidate_id': 'other'}):
            case = deepcopy(self.fixture['case']); candidates = packet._json(case['context']['results'][0]['payload_json'])
            candidates[0].update(identity)
            self.assert_bad_case(replace_candidates(case, candidates))

    def test_rehashed_modified_formula_and_candidate_attribution_are_rejected(self):
        for where, key, value in [('candidate', 'expression', '(close / open)'),
                                  ('result', 'expression', '(close / open)'),
                                  ('candidate', 'origin', 'model_conjecture'),
                                  ('candidate', 'changes', ['Unreviewed alteration'])]:
            case = deepcopy(self.fixture['case']); candidates = packet._json(case['context']['results'][0]['payload_json'])
            target = candidates[0] if where == 'candidate' else candidates[0]['result']
            target[key] = value
            self.assert_bad_case(replace_candidates(case, candidates))

    def test_rehashed_source_page_formula_or_hypothesis_misattribution_rejected(self):
        variants = []
        for key, value in [('text', '(close / open)'), ('locator', 'Wrong p.14; PDF SHA256 ' + packet.PAPER_SHA256),
                           ('verification', 'model_generated'), ('origin', 'model')]:
            case = deepcopy(self.fixture['case']); case['context']['evidence'][0][key] = value; variants.append(rebind_case(case))
        case = deepcopy(self.fixture['case'])
        definition = packet._json(case['context']['definitions'][0]['text']); definition['mechanism_attribution'] = 'paper_original'
        case['context']['definitions'][0]['text'] = json_text(definition); variants.append(rebind_case(case))
        for case in variants:
            self.assert_bad_case(case)

    def test_nonvalidation_real_or_legacy_mode_rejected_after_rehash(self):
        for key, value in [('split', 'test'), ('split', 'train'), ('market_metadata', {'data_kind': 'real'})]:
            case = deepcopy(self.fixture['case']); candidates = packet._json(case['context']['results'][0]['payload_json'])
            candidates[0]['result'][key] = value
            self.assert_bad_case(replace_candidates(case, candidates))
        case = deepcopy(self.fixture['case'])
        next(item for item in case['context']['provenance'] if item['label'] == 'mode')['value'] = 'legacy'
        self.assert_bad_case(rebind_case(case))

    def test_material_fixed_version_source_digest_and_quote_rejected(self):
        for path, value in [(('material_sha256',), 'f' * 64), (('material_case_digest',), 'f' * 64),
                            (('sources', 0, 'sha256'), 'f' * 64), (('evidence', 0, 'page'), 14),
                            (('evidence', 0, 'quote'), '(close / open)'), (('proposal', 'claim'), 'New invented economic fact')]:
            material = deepcopy(self.fixture['material']); target = material
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            self.assert_bad_case(self.fixture['case'], material)

    def test_wrong_case_result_digest_and_noncanonical_payload_rejected(self):
        case = deepcopy(self.fixture['case']); case['digest'] = 'f' * 64; self.assert_bad_case(case)
        case = deepcopy(self.fixture['case']); case['context']['results'][0]['digest'] = 'f' * 64; self.assert_bad_case(rebind_case(case))
        case = deepcopy(self.fixture['case']); case['context']['results'][0]['payload_json'] += ' '; self.assert_bad_case(rebind_case(case))
        case = deepcopy(self.fixture['case']); case['context']['results'][0]['payload_json'] = '[{"id":"alpha101","id":"other"}]'
        self.assert_bad_case(rebind_case(case))

    def test_rehashed_metric_claim_forgery_and_wrong_pointer_rejected(self):
        for key, value in [('value', 999999.0), ('verification', 'human_approved'), ('display', 'Forged = 999999')]:
            value_packet = deepcopy(self.snapshot)
            value_packet['claims']['claims'][1]['metrics'][0][key] = value
            body = {name: value_packet['claims'][name] for name in CLAIM_FIELDS}
            value_packet['claims']['digest'] = digest(body); value_packet['claims']['id'] = 'research_claims_' + digest(body)
            self.assert_bad_packet(value_packet)
        value_packet = deepcopy(self.snapshot)
        value_packet['claims']['submitted_claims'][1]['metric_references'][0]['pointer'] = '/0/result/metrics/mean_gross_return'
        self.assert_bad_packet(value_packet)

    def test_rehashed_claim_text_approval_and_foreign_targets_rejected(self):
        for key, value in [('narrative_text', 'Unbacked new conclusion'), ('actor', 'human'), ('semantic_fidelity', 'passed')]:
            value_packet = deepcopy(self.snapshot); value_packet['claims']['claims'][0][key] = value
            self.assert_bad_packet(value_packet)
        for key, value in [('claim_digest', 'f' * 64), ('case_context_digest', 'f' * 64), ('source_id', 'wrong-run')]:
            value_packet = deepcopy(self.snapshot); target = value_packet['targets'][0]; target[key] = value
            target['target_digest'] = digest({name: item for name, item in target.items() if name != 'target_digest'})
            self.assert_bad_packet(value_packet)
        value_packet = deepcopy(self.snapshot); value_packet['targets'][1] = deepcopy(value_packet['targets'][0])
        self.assert_bad_packet(value_packet)

    def test_runtime_exact_binding_and_ai_state_rejected(self):
        for path, value in [(('health', 'workspace_id'), 'not-a-workspace'), (('health', 'ai_enabled'), True),
                            (('health', 'database_schema'), 15), (('runtime', 'protocol', 'task_sha256'), 'f' * 64),
                            (('runtime', 'selected', 'run_id'), 'wrong-run'), (('runtime', 'selected', 'attempt_id'), 'wrong-attempt'),
                            (('runtime', 'selected', 'verified'), False), (('runtime', 'runtime', 'code_commit'), 'not-a-valid-commit')]:
            value_packet = deepcopy(self.snapshot); target = value_packet
            for key in path[:-1]: target = target[key]
            target[path[-1]] = value
            self.assert_bad_packet(value_packet)

    def test_shared_preflight_rejects_false_compatible_declarations(self):
        inputs = {key: self.fixture[key] for key in ('case', 'material', 'runtime', 'health')}
        report = packet.validate_inputs(**inputs)
        self.assertTrue(report['passed'])
        for section, key, changed in [('protocol', 'id', 'foreign-protocol'), ('protocol', 'digest', 'f' * 64),
                                      ('protocol', 'task_sha256', 'f' * 64),
                                      ('bound_outputs', 'run_id', 'wrong-run'), ('bound_outputs', 'task_sha256', 'f' * 64)]:
            frozen = deepcopy(inputs)
            target = (frozen['runtime']['protocol'] if section == 'protocol'
                      else frozen['runtime']['selected']['bound_outputs'])
            target[key] = changed
            with self.assertRaises(ValueError):
                packet.validate_inputs(**frozen)

    def test_packet_checksum_scope_selection_and_human_injection_rejected(self):
        value_packet = deepcopy(self.snapshot); value_packet['packet_digest'] = 'f' * 64
        with self.assertRaises(ValueError): packet.verify_packet(value_packet)
        for key, value in [('human_judgments', {'passed': True}), ('semantic_quality_score', 1.0), ('llm_api_called', True),
                           ('scope', 'real_market_reproduction'), ('schema_version', True)]:
            value_packet = deepcopy(self.snapshot); value_packet[key] = value; self.assert_bad_packet(value_packet)
        value_packet = deepcopy(self.snapshot); value_packet['selection']['candidate_index'] = 1; self.assert_bad_packet(value_packet)
        value_packet = deepcopy(self.snapshot); value_packet['unbounded_extra'] = True; self.assert_bad_packet(value_packet)

    def test_targets_order_is_normalized_and_inputs_are_not_mutated(self):
        inputs = {key: deepcopy(self.fixture[key]) for key in ('case', 'material', 'claims', 'targets', 'runtime', 'health')}
        inputs['targets'].reverse(); before = deepcopy(inputs)
        result = packet.build_packet(**inputs)
        self.assertEqual(inputs, before)
        self.assertEqual(result, self.snapshot)
        result['claims']['claims'][0]['narrative_text'] = 'Mutated output'
        self.assertEqual(inputs, before)

    def test_oversized_input_rejected_before_typed_projection(self):
        with patch.object(packet, 'MAX_PACKET_BYTES', 100):
            with self.assertRaises(ValueError): packet.verify_packet(self.snapshot)
            with self.assertRaises(ValueError): packet.build_claim_request(self.fixture['case'], self.fixture['material'])


if __name__ == '__main__':
    unittest.main()
