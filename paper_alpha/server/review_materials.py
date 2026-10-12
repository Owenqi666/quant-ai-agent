"""Exact-claim read-only source access. Paths are derived, never caller supplied."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .. import semantic_materials
from ..review_material_packet import (AUTHOR_IDS, LIMITATIONS, MAX_JSON, MAX_SOURCE,
                                      decode, safe_read, source_url, verify_packet)
from ..storage import digest, json_text
from .claim_reviews import ClaimReviews
from .research_cases import ResearchCases
from .research_claims import ResearchClaims
from .review_materials_schema import ReviewMaterialsDetail
from .service import ServiceError


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ServiceError('Invalid source attempt identity', 409)
    return value


class ReviewMaterials:
    def __init__(self, store):
        self.store = store
        self.claim_reviews = ClaimReviews(store)

    def _target(self, claims_id, claim_id, expected_target_digest):
        if not isinstance(expected_target_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_target_digest):
            raise ServiceError('Material access requires an exact target SHA256', 422)
        target = self.claim_reviews.target(claims_id, claim_id)
        if target['target_digest'] != expected_target_digest:
            raise ServiceError('Review material target changed; refresh the exact claim first', 412)
        if target['source_kind'] not in {'daily_run', 'industry_mom_experiment'}:
            raise ServiceError('Original material access supports Alpha101 daily and fixed industry MOM only', 422)
        return target

    def _sources(self, case, target):
        """Return exact bounded bytes with whitelist identity, no raw DB paths."""
        files = []
        evidence = case['context']['evidence']
        if case['source_kind'] == 'daily_run':
            run = self.store.get_run(case['source_id'])
            if run['status'] != 'completed' or not run.get('verification', {}).get('verified') or not run.get('regression_target'):
                raise ValueError('Daily source is not a completed verified attempt')
            attempt_id = _uuid(run['regression_target']['attempt_id'])
            base = self.store.root / 'runs' / _uuid(case['source_id']) / 'attempts' / attempt_id / 'output'
            attempt = self.store._fetch('attempts', attempt_id)
            if Path(attempt['output_dir']).absolute() != base.absolute() or attempt['run_id'] != case['source_id']:
                raise ValueError('Daily attempt does not own its derived source path')
            manifest = decode(safe_read(base / 'manifest.json', MAX_JSON))
            expected = semantic_materials.SOURCE_FILES['alpha101_paper'][1]
            if (manifest['snapshot_files'].get('inputs/paper.pdf') != expected
                    or manifest['signature']['inputs'].get('paper_pdf') != expected
                    or not evidence or any('PDF SHA256 ' + expected not in e['locator'] for e in evidence if e['origin'] == 'paper')):
                raise ValueError('Daily source does not bind the fixed Alpha101 PDF')
            files.append(('alpha101-paper', 'Alpha101 原论文（冻结输入副本）', 'alpha101-paper.pdf',
                          'application/pdf', base / 'inputs/paper.pdf', expected,
                          [e['id'] for e in evidence if e['origin'] == 'paper']))
        else:
            from .industry_mom import IndustryMomExperiments
            service = IndustryMomExperiments(self.store)
            detail = service.get(case['source_id'])
            if (detail['experiment']['status'] != 'completed' or detail['verification'] is None
                    or detail['verification']['verified'] is not True or detail['review_target'] is None):
                raise ValueError('Industry source requires a completed verified attempt')
            source = detail['source']; method = json.loads(source['method_json']); config = json.loads(source['config_json'])
            base = service.sources._path(source['id']) / 'inputs'
            if (decode(safe_read(base / 'method.json', MAX_JSON)) != method
                    or decode(safe_read(base / 'config.json', MAX_JSON)) != config):
                raise ValueError('Registered method/config bytes differ from verified source')
            files.extend([
                ('industry-paper', 'Momentum 原论文（已注册来源副本）', 'momentum-paper.pdf', 'application/pdf',
                 base / 'paper.pdf', method['paper']['document_sha256'], [e['id'] for e in evidence if e['origin'] == 'paper']),
                ('industry-method', '固定行业研究方法及原文／项目选择归因', 'method.json', 'application/json',
                 base / 'method.json', None, [e['id'] for e in evidence]),
                ('industry-config', '固定行业计算配置（开发区间）', 'config.json', 'application/json',
                 base / 'config.json', None, ['industry-project-method']),
            ])
            authors = {item['id']: item for item in method['sources']}
            if set(authors) != set(AUTHOR_IDS):
                raise ValueError('Author source whitelist differs from fixed contract')
            for name in AUTHOR_IDS:
                files.append(('industry-author-' + name, '作者代码 ' + name + '.m（仅读，不执行）', name + '.m',
                              'text/plain', base / 'author-code' / (name + '.m'), authors[name]['sha256'], ['industry-author-' + name]))
        descriptors, contents, paths = [], {}, {}
        total = 0
        for identity, label, filename, media_type, path, expected, evidence_ids in files:
            value = safe_read(path, MAX_SOURCE if media_type == 'application/pdf' else MAX_JSON)
            total += len(value)
            if total > 32 * 1024 * 1024 or len(files) > 16:
                raise ValueError('Material sources exceed their byte/file bound')
            actual = _sha(value)
            if expected is not None and actual != expected:
                raise ValueError('Material bytes differ from original source digest')
            descriptors.append({'id': identity, 'label': label, 'filename': filename, 'media_type': media_type,
                                'sha256': actual, 'size': len(value), 'download_url': source_url(target, identity),
                                'evidence_ids': evidence_ids})
            contents[identity], paths[identity] = value, path
        return descriptors, contents, paths

    def _snapshot(self, claims_id, claim_id, expected_target_digest):
        target = self._target(claims_id, claim_id, expected_target_digest)
        claims = ResearchClaims(self.store).get(claims_id)
        case = ResearchCases(self.store).get(target['case_id'])
        status = self.claim_reviews.status(claims_id, claim_id)
        records, expected_count = [], status['records']
        if expected_count > 500:
            raise ServiceError('Exact review history exceeds its 500-record bound', 413)
        for offset in range(0, expected_count, 100):
            page = self.claim_reviews.list(limit=100, offset=offset, claims_id=claims_id, claim_id=claim_id)
            if page['total'] != expected_count:
                raise ServiceError('Review history changed during material read; refresh', 409)
            records.extend(page['items'])
        records.sort(key=lambda record: (record['created_at'], record['id']))
        if len(records) != expected_count or any(record['target'] != target for record in records):
            raise ServiceError('Review history does not cover the exact target', 409)
        descriptors, contents, paths = self._sources(case, target)
        if self._target(claims_id, claim_id, expected_target_digest) != target or self.claim_reviews.status(claims_id, claim_id) != status:
            raise ServiceError('Exact source/review state changed during material projection', 409)
        body = {'schema_version': 1, 'kind': 'exact_claim_review_materials', 'case': case, 'claims': claims,
                'target': target, 'review_status': status, 'reviews': records, 'sources': descriptors,
                'human_judgments': None, 'semantic_quality_score': None, 'llm_api_called': False, 'limitations': LIMITATIONS}
        value = body | {'digest': digest(body)}
        if len(json_text(value).encode('utf8')) > MAX_JSON:
            raise ServiceError('Exact review materials exceed 8 MiB; no history is silently omitted', 413)
        verify_packet(value, contents)
        # Final file fence follows every source/target/status revalidation and
        # the pure projection check. Return the already captured exact bytes.
        for source in descriptors:
            if _sha(safe_read(paths[source['id']], MAX_SOURCE if source['media_type'] == 'application/pdf' else MAX_JSON)) != source['sha256']:
                raise ValueError('Source changed at final material read fence')
        return ReviewMaterialsDetail.model_validate(value).model_dump(exclude_unset=True), contents

    def get(self, claims_id, claim_id, expected_target_digest):
        try:
            return deepcopy(self._snapshot(claims_id, claim_id, expected_target_digest)[0])
        except ServiceError:
            raise
        except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError) as exc:
            raise ServiceError('Review material source/target failed integrity verification', 409) from exc

    def source(self, claims_id, claim_id, expected_target_digest, source_id):
        if source_id not in {'alpha101-paper', 'industry-paper', 'industry-method', 'industry-config',
                            *('industry-author-' + name for name in AUTHOR_IDS)}:
            raise ServiceError('Unknown review material source identity', 404)
        try:
            material, contents = self._snapshot(claims_id, claim_id, expected_target_digest)
            source = next((s for s in material['sources'] if s['id'] == source_id), None)
            if source is None:
                raise ServiceError('Requested source does not belong to this exact target', 404)
            return {'content': contents[source_id], **{key: source[key] for key in ('media_type', 'filename', 'sha256')}}
        except ServiceError:
            raise
        except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError) as exc:
            raise ServiceError('Review source failed bounded exact-target verification', 409) from exc
