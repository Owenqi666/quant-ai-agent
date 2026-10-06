"""Real service projections, isolated synthetic computations and test labels only.

No live workspace, actual human judgment, provider call or reserved evaluation.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from paper_alpha import research_evaluation as evaluation
from paper_alpha import review_material_packet as material_api
from paper_alpha.server.claim_reviews import ClaimReviews
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.server.research_cases import CASE_FIELDS
from paper_alpha.server.research_claims import ResearchClaims, _resolve
from paper_alpha.server.review_materials import ReviewMaterials
from paper_alpha.storage import atomic_json, digest, read_json
from scripts import prepare_case_review
import tests.test_human_review_packet as daily_fixture
import tests.test_industry_mom_binding as industry_fixture


def material(store, claims, claim_id):
    target = ClaimReviews(store).target(claims['id'], claim_id)
    service = ReviewMaterials(store)
    # The service's full read path captures and verifies all bytes together;
    # avoid repeating the expensive industry source/attempt verification for
    # each of the nine files. HTTP per-file behavior is covered by Agent A.
    packet, sources = service._snapshot(claims['id'], claim_id, target['target_digest'])
    return packet, sources


def export_material(packet, sources, out, health):
    """Use the actual GET-only exporter with actual service-generated responses."""
    class Client:
        base_url = 'http://127.0.0.1:8765'
        calls = 0
        def request(self, method, path, **options):
            assert method == 'GET'
            self.calls += 1
            if path == '/api/health':
                return deepcopy(health)
            if path.startswith('/api/claim-review-targets/'):
                return deepcopy(packet['target'])
            if '/sources/' in path:
                return sources[urlsplit(path).path.rsplit('/', 1)[1]]
            params = parse_qs(urlsplit(path).query)
            assert params['claim_id'] == [packet['target']['claim_id']]
            assert params['expected_target_digest'] == [packet['target']['target_digest']]
            return deepcopy(packet)
    with patch.object(prepare_case_review, 'Client', return_value=Client()):
        prepare_case_review.prepare(Client.base_url, health['workspace_id'], packet['claims']['id'],
            packet['target']['claim_id'], out, expected_target_digest=packet['target']['target_digest'])
    return out


def rehash_packet(packet):
    packet['digest'] = digest({k: v for k, v in packet.items() if k != 'digest'})
    return packet


def rebind_case_and_claims(packet, edit_result=None):
    """Completely rehash untrusted input to test linkage beyond plain checksum."""
    case, claims = packet['case'], packet['claims']
    if edit_result:
        result = case['context']['results'][0]
        payload = json.loads(result['payload_json']); edit_result(payload)
        result['payload_json'] = json.dumps(payload); result['digest'] = digest(payload)
    case['source_digest'] = digest({key: case[key] for key in ('source_kind', 'source_id', 'context')})
    case['digest'] = digest({key: case[key] for key in CASE_FIELDS}); case['id'] = 'research_case_' + case['digest']
    claims['case_id'], claims['case_digest'] = case['id'], case['digest']
    result = case['context']['results'][0]
    for draft in claims['submitted_claims']:
        for ref in draft['metric_references']:
            ref.update(case_id=case['id'], case_digest=case['digest'], result_id=result['id'], result_digest=result['digest'])
        resolved = next(item for item in claims['claims'] if item['id'] == draft['id'])
        resolved['metrics'] = [_resolve(result, ref) for ref in draft['metric_references']]
        resolved['authoritative_display'] = '\n'.join(m['display'] for m in resolved['metrics']) if resolved['metrics'] else None
    claims['digest'] = digest(evaluation._body(claims)); claims['id'] = 'research_claims_' + claims['digest']
    target = packet['target']; claim = next(item for item in claims['claims'] if item['id'] == target['claim_id'])
    target.update(claims_id=claims['id'], claims_digest=claims['digest'], case_id=case['id'], case_digest=case['digest'],
        claim_digest=digest(claim), claim=deepcopy(claim), source_kind=case['source_kind'], source_id=case['source_id'],
        source_digest=case['source_digest'], case_context_digest=digest(case['context']),
        **{key: deepcopy(case['context'][key]) for key in ('evidence', 'definitions', 'provenance', 'data_scope', 'method_scope', 'stop_reason')})
    target['results'] = [{key: r[key] for key in ('id', 'kind', 'digest', 'summary')} for r in case['context']['results']]
    target['target_digest'] = digest({key: value for key, value in target.items() if key != 'target_digest'})
    packet['review_status'] = material_api._status(target, [])
    for source in packet['sources']:
        source['download_url'] = material_api.source_url(target, source['id'])
    return rehash_packet(packet)


class DailyResearchEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve()
        cls.fixture = daily_fixture.build_fixture(cls.root / 'daily')
        cls.multiple = daily_fixture.build_fixture(cls.root / 'multiple', single=False)

    def setUp(self):
        self.store = self.fixture['store']; self.claims = ResearchClaims(self.store); self.reviews = ClaimReviews(self.store)
        request = deepcopy(self.fixture['request'])
        request['claims'][1]['text'] += ' Isolated test ' + self._testMethodName
        self.batch = self.claims.create(**request, idempotency_key=self._testMethodName)
        self.claim_id = request['claims'][1]['id']
        self.packet, self.sources = material(self.store, self.batch, self.claim_id)

    def review(self, source, reviewer, outcome, key, parent=None, changes=None):
        target = self.reviews.target(self.batch['id'], self.claim_id)
        dims = {name: {'outcome': outcome, 'reason': 'Controlled fixture; not a real human judgment.'} for name in DIMENSIONS}
        if changes:
            for name, value in changes.items():
                dims[name]['outcome'] = value
        return self.reviews.create(claims_id=self.batch['id'], claim_id=self.claim_id,
            expected_target_digest=target['target_digest'], source=source, reviewer=reviewer,
            confirmed_at='2020-01-01T00:00:00+00:00', dimensions=dims,
            supersedes_id=parent['id'] if parent else None, expected_supersedes_digest=parent['digest'] if parent else None,
            idempotency_key=self._testMethodName + '-' + key)

    def report(self):
        packet, sources = material(self.store, self.batch, self.claim_id)
        return evaluation.evaluate_sample(evaluation.build_sample(packet, source_files=sources), source_files=sources)

    def bad(self, packet, sources=None):
        with self.assertRaises((ValueError, evaluation.ResearchEvaluationError)):
            evaluation.build_sample(packet, source_files=self.sources if sources is None else sources)

    def test_legacy_case_review_defaults_preserve_exact_snapshot_identity(self):
        # Old experiment-level reviews lack later assessment fields. Their
        # absence is part of the frozen Case and never supplies ClaimReview.
        legacy = {'id': 'legacy-controlled-review', 'actor': 'human',
            'decision': 'needs_changes', 'scope': 'Controlled legacy fixture; not an exact claim review.',
            'note': 'Historical schema compatibility only; no real human judgment.'}
        for explicit_null in (False, True):
            with self.subTest(explicit_null=explicit_null):
                packet = deepcopy(self.packet)
                review = legacy | ({'assessment': None, 'assessment_summary': None} if explicit_null else {})
                packet['case']['context']['reviews'] = [review]
                rebind_case_and_claims(packet)
                expected = deepcopy(packet)
                sample = evaluation.build_sample(packet, source_files=self.sources)
                self.assertEqual(sample['material'], expected)
                self.assertEqual(sample['digest'], digest(evaluation._body(sample, ('id', 'digest'))))
                roundtrip = json.loads(json.dumps(sample))
                self.assertTrue(evaluation.verify_sample(roundtrip, source_files=self.sources)['passed'])
                report = evaluation.evaluate_sample(roundtrip, source_files=self.sources)
                self.assertEqual(report['technical_status'], 'passed')
                self.assertEqual(report['human_reference']['human_records'], 0)
                self.assertEqual(report['human_reference']['status'], 'pending')
                self.assertEqual(packet, expected)
                # Rehashing the container does not authorize changing an exact
                # historical Case by adding/removing default fields.
                tampered = deepcopy(sample)
                changed = tampered['material']['case']['context']['reviews'][0]
                if explicit_null:
                    del changed['assessment']; del changed['assessment_summary']
                else:
                    changed.update(assessment=None, assessment_summary=None)
                rehash_packet(tampered['material'])
                tampered['digest'] = digest(evaluation._body(tampered, ('id', 'digest')))
                tampered['id'] = 'research_sample_' + tampered['digest']
                with self.assertRaisesRegex(ValueError, 'Stored content identity differs'):
                    evaluation.verify_sample(tampered, source_files=self.sources)

    def test_daily_actual_metrics_and_unjudged_draft_without_model(self):
        sample = evaluation.build_sample(self.packet, source_files=self.sources)
        report = evaluation.evaluate_sample(sample, source_files=self.sources)
        self.assertEqual(report['domain'], 'daily_alpha101'); self.assertEqual(report['technical_status'], 'passed')
        original = json.loads(self.packet['case']['context']['results'][0]['payload_json'])[0]['result']['metrics']
        for metric in report['metrics']:
            self.assertEqual(metric['value'], original[metric['pointer'].rsplit('/', 1)[1]])
            self.assertIn(metric['display'], evaluation.report_markdown(report))
        self.assertEqual(report['human_reference']['status'], 'pending')
        self.assertEqual(report['human_reference']['human_records'], 0)
        self.assertEqual(report['declaration_comparison']['status'], 'not_evaluated_no_prediction')
        self.assertIsNone(report['model_accuracy']); self.assertIsNone(report['semantic_quality_score'])
        self.assertEqual(report['model_execution'], 'model_not_run'); self.assertFalse(report['reserved_evaluated'])

    def test_alpha101_candidate_not_zero_is_selected_by_identity(self):
        packet, sources = material(self.multiple['store'], self.multiple['claims'], self.multiple['request']['claims'][1]['id'])
        report = evaluation.evaluate_sample(evaluation.build_sample(packet, source_files=sources), source_files=sources)
        self.assertTrue(all(metric['pointer'].startswith('/1/') for metric in report['metrics']))
        self.assertEqual(report['domain'], 'daily_alpha101')
        wrong = deepcopy(packet)
        wrong['claims']['submitted_claims'][1]['metric_references'][0]['pointer'] = '/0/result/metrics/mean_rank_ic'
        wrong = rebind_case_and_claims(wrong)
        with self.assertRaisesRegex(ValueError, 'different candidate'):
            evaluation.build_sample(wrong, source_files=sources)

    def test_missing_source_bytes_are_explicitly_incomplete(self):
        sample = evaluation.build_sample(self.packet)
        report = evaluation.evaluate_sample(sample)
        self.assertEqual(report['technical_status'], 'incomplete')
        self.assertEqual(report['source_integrity_scope'], 'source_bytes_not_checked')
        self.assertEqual(report['checks'][-1]['status'], 'not_checked')
        self.bad(self.packet, {})
        altered = deepcopy(self.sources); altered[next(iter(altered))] += b'changed'; self.bad(self.packet, altered)

    def test_wrong_digest_target_pointer_numeric_value_or_evidence_rejected(self):
        variants = []
        p = deepcopy(self.packet); p['digest'] = '0' * 64; variants.append(p)
        p = deepcopy(self.packet); p['target']['target_digest'] = '0' * 64; variants.append(rehash_packet(p))
        p = deepcopy(self.packet); p['claims']['claims'][1]['metrics'][0]['value'] = 999999; variants.append(rehash_packet(p))
        p = deepcopy(self.packet); p['claims']['submitted_claims'][1]['metric_references'][0]['pointer'] = '/0/result/execution'; variants.append(rehash_packet(p))
        p = deepcopy(self.packet); p['claims']['submitted_claims'][0]['evidence_ids'] = ['foreign']; variants.append(rehash_packet(p))
        p = deepcopy(self.packet); p['llm_api_called'] = True; variants.append(rehash_packet(p))
        for value in variants:
            with self.subTest(mutation=variants.index(value)):
                self.bad(value)

    def test_rehashed_nonvalidation_or_wrong_configuration_rejected(self):
        for edit in (lambda p: p[0]['result'].update(split='test'),
                     lambda p: p[0]['result']['config'].update(min_assets=99),
                     lambda p: p[0].update(id='other-alpha')):
            p = rebind_case_and_claims(deepcopy(self.packet), edit)
            self.bad(p)

    def test_automation_only_cannot_supply_human_reference(self):
        self.review('automation', 'fixture automation', 'passed', 'automation')
        report = self.report(); ref = report['human_reference']
        self.assertEqual((ref['human_records'], ref['automation_records'], ref['eligible_dimensions']), (0, 1, 0))
        self.assertEqual(ref['status'], 'pending')
        self.assertIsNone(report['declaration_comparison']['declaration_agreement_rate'])

    def test_human_without_prediction_has_reference_but_no_accuracy(self):
        self.review('human', 'Synthetic control, not a person', 'passed', 'human')
        report = self.report()
        self.assertEqual(report['human_reference']['eligible_dimensions'], 5)
        self.assertEqual(report['human_reference']['status'], 'available')
        self.assertEqual(report['declaration_comparison']['status'], 'not_evaluated_no_prediction')
        self.assertIsNone(report['model_accuracy'])

    def test_unknown_and_conflicting_dimensions_are_not_scored(self):
        first = DIMENSIONS[0]
        self.review('human', 'Synthetic H1', 'passed', 'h1', changes={first: 'not_assessed'})
        self.review('automation', 'Synthetic A', 'passed', 'a')
        report = self.report(); self.assertEqual(report['human_reference']['status'], 'unknown')
        self.assertEqual(report['human_reference']['unknown_dimensions'], 1)
        self.assertEqual(report['declaration_comparison']['comparable_dimensions'], 4)
        self.review('human', 'Synthetic H2', 'passed', 'h2', changes={first: 'failed'})
        report = self.report(); self.assertEqual(report['human_reference']['status'], 'conflicting')
        self.assertEqual(report['human_reference']['conflicting_dimensions'], 1)
        self.assertEqual(report['declaration_comparison']['matched_dimensions'], 4)

    def test_superseded_human_history_preserved_and_accurate_agreement(self):
        old = self.review('human', 'Synthetic same reviewer', 'failed', 'old')
        new = self.review('human', 'Synthetic same reviewer', 'passed', 'new', old)
        self.review('automation', 'Synthetic A', 'passed', 'a', changes={DIMENSIONS[0]: 'failed'})
        report = self.report(); ref = report['human_reference']
        self.assertEqual(ref['human_records'], 2); self.assertEqual(ref['active_human'], [{'id': new['id'], 'digest': new['digest']}])
        self.assertEqual(ref['superseded'], [{'id': old['id'], 'digest': old['digest']}])
        self.assertEqual(report['declaration_comparison']['declaration_agreement_rate'], 4 / 5)
        self.assertIsNone(report['model_accuracy'])

    def test_missing_parent_wrong_status_and_cross_target_review_rejected(self):
        old = self.review('human', 'Synthetic H', 'failed', 'old')
        self.review('human', 'Synthetic H', 'passed', 'new', old)
        packet, sources = material(self.store, self.batch, self.claim_id)
        bad = deepcopy(packet); bad['reviews'] = [r for r in bad['reviews'] if r['id'] != old['id']]; self.bad(rehash_packet(bad), sources)
        bad = deepcopy(packet); bad['review_status']['active_human_review_ids'] = []; self.bad(rehash_packet(bad), sources)
        bad = deepcopy(packet); bad['reviews'][0]['target']['claim_id'] = 'another'; self.bad(rehash_packet(bad), sources)

    def test_review_chain_counts_root_in_100_node_limit(self):
        seed = self.review('automation', 'Synthetic bounded chain', 'not_assessed', 'seed')
        records = []
        for index in range(101):
            record = deepcopy(seed)
            record['dimensions'][DIMENSIONS[0]]['reason'] = 'Synthetic bounded chain node ' + str(index)
            record['supersedes_id'] = records[-1]['id'] if records else None
            record['supersedes_digest'] = records[-1]['digest'] if records else None
            record['digest'] = digest(evaluation._body(record)); record['id'] = 'claim_review_' + record['digest']
            records.append(record)
        p = deepcopy(self.packet)
        p['reviews'] = sorted(records[:100], key=lambda record: (record['created_at'], record['id']))
        p['review_status'] = material_api._status(p['target'], p['reviews']); rehash_packet(p)
        self.assertTrue(evaluation.build_sample(p, source_files=self.sources))
        p['reviews'] = sorted(records, key=lambda record: (record['created_at'], record['id']))
        p['review_status'] = material_api._status(p['target'], p['reviews']); rehash_packet(p)
        with self.assertRaises(ValueError): evaluation.build_sample(p, source_files=self.sources)

    def bundle(self):
        root = self.root / self._testMethodName; root.mkdir()
        source = export_material(self.packet, self.sources, root / 'material-input', self.fixture['health'])
        output = root / 'sample'
        return root, source, output

    def test_export_relocate_and_cli_verify_from_unrelated_workdir(self):
        root, source, output = self.bundle()
        before = evaluation._inventory(source)
        accepted = evaluation.export_sample(source, output)
        self.assertTrue(accepted['passed']); self.assertEqual(evaluation._inventory(source), before)
        moved = root / 'moved'; shutil.copytree(output, moved)
        self.assertEqual(evaluation.verify_bundle(moved), accepted)
        unrelated = root / 'unrelated-working-directory'
        unrelated.mkdir()
        env = os.environ | {'PYTHONPATH': str(evaluation.ROOT)}
        run = subprocess.run([sys.executable, '-B', str(evaluation.ROOT / 'scripts/evaluate_research_sample.py'),
            'verify', '--out', str(moved)], cwd=unrelated, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
        self.assertEqual(json.loads(run.stdout), accepted)
        self.assertEqual(evaluation._inventory(moved), evaluation._inventory(output))
        with self.assertRaises(FileExistsError):
            evaluation.export_sample(source, output)

    def test_report_and_manifest_tampering_missing_file_links_and_extra_rehash_fail(self):
        root, source, output = self.bundle(); evaluation.export_sample(source, output)
        variants = ('report', 'source_missing', 'extra', 'invocation', 'link')
        for mutation in variants:
            copied = root / mutation; shutil.copytree(output, copied)
            if mutation == 'report':
                report = read_json(copied / 'report.json'); report['metrics'][0]['value'] = 999999
                report['digest'] = digest({k: v for k, v in report.items() if k != 'digest'}); atomic_json(copied / 'report.json', report)
            elif mutation == 'source_missing':
                (copied / 'source' / evaluation.SOURCE_FILES[0]).unlink()
            elif mutation == 'extra':
                (copied / 'extra.txt').write_text('unregistered even if outer hashes recomputed')
            elif mutation == 'invocation':
                invocation = read_json(copied / 'invocation.json'); invocation['reserved_evaluated'] = True; atomic_json(copied / 'invocation.json', invocation)
            else:
                (copied / 'outside-link').symlink_to(source / 'packet.json')
            if mutation != 'link':
                manifest = read_json(copied / 'manifest.json'); inv = evaluation._inventory(copied); inv.pop('manifest.json')
                manifest['files'] = inv; manifest['digest'] = digest({k: v for k, v in manifest.items() if k != 'digest'})
                atomic_json(copied / 'manifest.json', manifest)
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, OSError)):
                evaluation.verify_bundle(copied)

    def test_failed_input_retained_and_retry_uses_new_output(self):
        root, source, output = self.bundle()
        original = (source / 'packet.json').read_bytes(); (source / 'packet.json').write_bytes(b'{"invalid": true}')
        with self.assertRaises(evaluation.ResearchEvaluationError):
            evaluation.export_sample(source, output)
        self.assertTrue((output / 'invocation.json').is_file()); self.assertTrue((output / 'error.json').is_file())
        self.assertEqual(read_json(output / 'input-material-packet.json'), {'invalid': True})
        (source / 'packet.json').write_bytes(original)
        accepted = evaluation.export_sample(source, root / 'retry'); self.assertTrue(accepted['passed'])
        self.assertFalse(read_json(output / 'error.json')['passed'])

    def test_output_link_injection_cannot_overwrite_external_file_or_directory(self):
        root, source, output = self.bundle()
        for kind in ('file', 'directory'):
            out = root / kind
            outside = root / ('outside-' + kind)
            outside.mkdir()
            canary = outside / 'research_evaluation.py'; canary.write_bytes(b'unchanged external canary')
            original = evaluation._read; injected = False
            def read(path, *args, **kwargs):
                nonlocal injected
                value = original(path, *args, **kwargs)
                if not injected and Path(path) == evaluation.ROOT / evaluation.SOURCE_FILES[0] and (out / 'material').is_dir():
                    parent = out / 'source' / 'paper_alpha'; parent.parent.mkdir()
                    if kind == 'file':
                        parent.mkdir(); (parent / 'research_evaluation.py').symlink_to(canary)
                    else:
                        parent.symlink_to(outside, target_is_directory=True)
                    injected = True
                return value
            with patch.object(evaluation, '_read', side_effect=read), self.assertRaises(evaluation.ResearchEvaluationError):
                evaluation.export_sample(source, out)
            self.assertTrue(injected)
            self.assertEqual(canary.read_bytes(), b'unchanged external canary')
            self.assertEqual(set(p.name for p in outside.iterdir()), {'research_evaluation.py'})
            self.assertFalse(read_json(out / 'error.json')['passed'])

    def test_duplicate_nonfinite_json_and_resource_budget_fail_closed(self):
        root = self.root / self._testMethodName; root.mkdir()
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'):
            file = root / 'input.json'; file.write_bytes(data)
            with self.assertRaises(ValueError): evaluation._json(file)
        with patch.object(evaluation, 'MAX_JSON', 10):
            self.bad(self.packet)


class IndustryResearchEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = industry_fixture.IndustryMomBindingTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        identity, _, _ = self.fixture.completed(); case = self.fixture.bind(identity)
        draft = self.fixture.metric(case)
        self.batch = self.fixture.claims.create(case['id'], case['digest'], [draft], 'evaluation-claims')
        self.packet, self.sources = material(self.fixture.store, self.batch, draft['id'])

    def test_real_industry_projection_has_project_scope_gross_and_pending_human(self):
        report = evaluation.evaluate_sample(evaluation.build_sample(self.packet, source_files=self.sources), source_files=self.sources)
        self.assertEqual(report['domain'], 'industry_mom'); self.assertEqual(report['technical_status'], 'passed')
        self.assertEqual(report['human_reference']['status'], 'pending'); self.assertFalse(report['reserved_evaluated'])
        original = json.loads(self.packet['case']['context']['results'][0]['payload_json'])
        self.assertEqual(report['metrics'][0]['value'], original['summary'][0]['mean_gross_return'])
        self.assertIn('industry_portfolio', self.packet['target']['data_scope'])
        self.assertIn('project modification', '\n'.join(report['limitations']))
        self.assertEqual(len(self.sources), 9)

    def test_industry_wrong_scope_reserved_config_or_cross_result_rejected(self):
        for edit in (lambda p: p.update(asset_kind='individual_stock'),
                     lambda p: p.update(reserved_evaluated=True),
                     lambda p: p.update(research_scope='paper_reproduction'),
                     lambda p: p.update(config_digest='0' * 64)):
            p = rebind_case_and_claims(deepcopy(self.packet), edit)
            with self.assertRaises(ValueError): evaluation.build_sample(p, source_files=self.sources)
        p = deepcopy(self.packet); p['case']['source_kind'] = 'controlled_fixture'
        with self.assertRaises(ValueError): evaluation.build_sample(rehash_packet(p), source_files=self.sources)

    def test_industry_source_bytes_method_config_or_author_tamper_rejected(self):
        for name in ('industry-paper', 'industry-method', 'industry-config', 'industry-author-SetupDataA'):
            sources = deepcopy(self.sources); sources[name] += b'changed'
            with self.subTest(source=name), self.assertRaises(ValueError):
                evaluation.build_sample(self.packet, source_files=sources)


if __name__ == '__main__':
    unittest.main()
