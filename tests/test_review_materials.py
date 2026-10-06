"""Exact materials against real isolated daily/industry tools, never human labels."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_alpha.review_material_packet import verify_packet, _status, LIMITATIONS
from paper_alpha.server.claim_reviews import LIMITATIONS as REVIEW_LIMITATIONS
from paper_alpha.server.research_assessments import DIMENSIONS
from paper_alpha.server.review_materials import ReviewMaterials
from paper_alpha.server.service import ServiceError
from paper_alpha.storage import digest
from tests.test_human_review_packet import build_fixture
import tests.test_industry_mom_binding as industry_fixture


def rehash(value):
    value['digest'] = digest({k: v for k, v in value.items() if k != 'digest'})
    return value


class ReviewMaterialsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.fixture = build_fixture(Path(cls.temp.name) / 'daily')
        cls.service = ReviewMaterials(cls.fixture['store'])
        cls.target = cls.fixture['targets'][1]
        cls.args = (cls.target['claims_id'], cls.target['claim_id'], cls.target['target_digest'])
        cls.packet, cls.contents = cls.service._snapshot(*cls.args)

    def test_real_daily_pdf_results_empty_judgments_and_no_writes(self):
        before = self.fixture['store']._read('SELECT * FROM claim_reviews')
        value = self.service.get(*self.args)
        self.assertTrue(verify_packet(value, self.contents)['source_files_verified'])
        self.assertFalse(verify_packet(value)['source_files_verified'])
        self.assertEqual(value['human_judgments'], None)
        self.assertEqual(value['review_status']['human_records'], 0)
        self.assertEqual(value['target']['claim']['metrics'], self.target['claim']['metrics'])
        source = self.service.source(*self.args, 'alpha101-paper')
        self.assertEqual(hashlib.sha256(source['content']).hexdigest(), value['sources'][0]['sha256'])
        self.assertEqual(before, self.fixture['store']._read('SELECT * FROM claim_reviews'))

    def test_wrong_target_and_unknown_or_foreign_sources_fail(self):
        for callback, status in [(lambda: self.service.get(*self.args[:2], 'f' * 64), 412),
            (lambda: self.service.get(*self.args[:2], 'bad'), 422),
            (lambda: self.service.source(*self.args, '../paper.pdf'), 404),
            (lambda: self.service.source(*self.args, 'industry-paper'), 404)]:
            with self.assertRaises(ServiceError) as caught: callback()
            self.assertEqual(caught.exception.status, status)

    def test_source_bytes_link_routes_and_media_tamper_are_rejected(self):
        altered = dict(self.contents); altered['alpha101-paper'] += b'changed'
        with self.assertRaises(ValueError): verify_packet(self.packet, altered)
        for key, value in [('filename', 'another.pdf'), ('media_type', 'text/plain'),
            ('download_url', 'http://example.com/source'), ('evidence_ids', [])]:
            changed = deepcopy(self.packet); changed['sources'][0][key] = value
            with self.assertRaises(ValueError): verify_packet(rehash(changed))

    def test_prefilled_human_and_modified_number_projection_rejected(self):
        changed = deepcopy(self.packet); changed['human_judgments'] = {'passed': True}
        with self.assertRaises(ValueError): verify_packet(rehash(changed))
        changed = deepcopy(self.packet); changed['claims']['claims'][1]['metrics'][0]['value'] = 999
        with self.assertRaises(ValueError): verify_packet(rehash(changed))
        changed = deepcopy(self.packet); changed['review_status']['human_records'] = 1
        with self.assertRaises(ValueError): verify_packet(rehash(changed))

    def test_service_final_source_read_fence_rejects_changed_bytes(self):
        import paper_alpha.server.review_materials as module
        original, count = module.safe_read, 0
        def read(path, limit=module.MAX_SOURCE):
            nonlocal count
            value = original(path, limit)
            if Path(path).name == 'paper.pdf':
                count += 1
                if count == 2: return value + b'changed-at-final-fence'
            return value
        with patch.object(module, 'safe_read', side_effect=read), self.assertRaises(ServiceError) as caught:
            self.service.get(*self.args)
        self.assertEqual(caught.exception.status, 409)

    def test_service_final_target_read_fence_rejects_drift(self):
        original, count = self.service._target, 0
        def target(*args):
            nonlocal count
            value = original(*args); count += 1
            if count == 2: value['data_scope'] += 'changed'
            return value
        with patch.object(self.service, '_target', side_effect=target), self.assertRaises(ServiceError):
            self.service.get(*self.args)

    def _chain(self, count):
        records, parent = [], None
        for i in range(count):
            stamp = (datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i)).isoformat()
            body = {'schema_version': 1, 'target': self.target, 'source': 'automation', 'reviewer': 'isolated chain fixture',
                'confirmed_at': stamp, 'dimensions': {n: {'outcome': 'not_assessed', 'reason': 'Synthetic chain boundary, no human label.'} for n in DIMENSIONS},
                'supersedes_id': parent['id'] if parent else None, 'supersedes_digest': parent['digest'] if parent else None,
                'declared_semantic_status': 'incomplete', 'identity_scope': 'local_declaration_not_authenticated',
                'original_result_approval': False, 'software_verified_semantic_truth': False, 'semantic_quality_score': None,
                'limitations': REVIEW_LIMITATIONS}
            sha = digest(body); parent = body | {'id': 'claim_review_' + sha, 'digest': sha, 'created_at': stamp}; records.append(parent)
        packet = deepcopy(self.packet); packet['reviews'] = records; packet['review_status'] = _status(self.target, records)
        return rehash(packet)

    def test_chain_bound_counts_root_and_preserves_full_ancestor_history(self):
        self.assertTrue(verify_packet(self._chain(100))['passed'])
        with self.assertRaises(ValueError): verify_packet(self._chain(101))
        changed = self._chain(3); changed['reviews'].pop(0)
        with self.assertRaises(ValueError): verify_packet(rehash(changed))

    def test_service_retains_automation_superseded_history_without_human_promotion(self):
        reviews = self.service.claim_reviews
        for i in range(2):
            target = self.fixture['targets'][0]
            parent = reviews.list(claims_id=target['claims_id'], claim_id=target['claim_id'])['items']
            parent = parent[-1] if parent else None
            request = {'claims_id': target['claims_id'], 'claim_id': target['claim_id'], 'expected_target_digest': target['target_digest'],
                'source': 'automation', 'reviewer': 'Synthetic controlled fixture', 'confirmed_at': f'2020-01-01T00:00:0{i}+00:00',
                'dimensions': {n: {'outcome': 'not_assessed', 'reason': 'Automation only.'} for n in DIMENSIONS},
                'supersedes_id': parent['id'] if parent else None, 'expected_supersedes_digest': parent['digest'] if parent else None,
                'idempotency_key': f'materials-automation-{i}'}
            reviews.create(**request)
        value = self.service.get(target['claims_id'], target['claim_id'], target['target_digest'])
        self.assertEqual(len(value['reviews']), 2)
        self.assertEqual(value['review_status']['human_records'], 0)
        self.assertEqual(value['review_status']['superseded_records'], 1)
        self.assertEqual(value['review_status']['human_declared_status'], 'pending')


class IndustryReviewMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = industry_fixture.IndustryMomBindingTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        identity, _, _ = self.fixture.completed()
        case = self.fixture.bind(identity)
        claims = self.fixture.claims.create(case['id'], case['digest'], [self.fixture.metric(case)], 'materials-claims')
        target = self.fixture.reviews.target(claims['id'], 'gross-metric')
        self.service = ReviewMaterials(self.fixture.store); self.args = (claims['id'], 'gross-metric', target['target_digest'])

    def test_industry_exact_nine_sources_actual_result_and_no_semantic_claim(self):
        packet, contents = self.service._snapshot(*self.args)
        verdict = verify_packet(packet, contents)
        self.assertTrue(verdict['passed']); self.assertEqual(len(contents), 9)
        self.assertIsNone(verdict['semantic_quality_score']); self.assertEqual(verdict['human_records'], 0)
        self.assertIn('Project modification', packet['target']['method_scope'])
        self.assertEqual(packet['target']['claim']['metrics'][0]['value'], packet['claims']['claims'][0]['metrics'][0]['value'])
        self.assertEqual(contents['industry-paper'], self.fixture.fixture.paper_bytes)
        self.assertTrue(all(not v.get('human_judgment') for v in [packet]))
        for identity in ('industry-method', 'industry-config', 'industry-author-main'):
            self.assertTrue(contents[identity])
        self.assertEqual(self.fixture.store._read('SELECT * FROM claim_reviews'), [])
