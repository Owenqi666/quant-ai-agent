"""Explicit fixed-MOM review, immutable plan and recoverable queue handoff.

GET readers never publish files or acquire execution authority. HTTP mutation
keys have independent immutable sidecars; the original bridge still owns the
one deterministic monthly queue key. No model call or database schema is added.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import re
import stat
import time
from types import SimpleNamespace

from fastapi import APIRouter, Query
from ..storage import digest, json_text
from .provider_industry_mom_bridge import IndustryMomMaterialBridge, _safe, _read, _put
from .provider_api import _PublicViews
from .paper_segments import MaterialError
from .service import ServiceError
from .response_schemas import ErrorResponse
from .industry_mom_material_schema import (
    SHA, PLAN_ID, IndustryMomMaterialPreview, IndustryMomMaterialCreate,
    IndustryMomMaterialStart, IndustryMomMaterialPlan, IndustryMomMaterialPlanCreated,
    IndustryMomMaterialStarted, IndustryMomMaterialPlanPage,
    IndustryMomMaterialLineage, IndustryMomExperimentMaterialLineage)

MAX_PLANS = 256
MAX_RECEIPTS = 4096
MAX_PUBLIC_BYTES = 512 * 1024
PLAN_RE = re.compile(PLAN_ID)


def _require(condition, message, code='MONTHLY_MATERIAL_INTEGRITY', status=409):
    if not condition:
        raise MaterialError(message, status, code=code)


class IndustryMomMaterialPlans:
    def __init__(self, store):
        self.store = store
        self.bridge = IndustryMomMaterialBridge(store)
        self.public = _PublicViews(store, SimpleNamespace(configs={}), None)

    def _call(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except ServiceError as exc:
            raise MaterialError(self.public.text(str(exc)), exc.status,
                                code=getattr(exc, 'code', 'MONTHLY_MATERIAL_INTEGRITY')) from exc
        except FileNotFoundError as exc:
            raise MaterialError('The frozen monthly material resource was not found', 404,
                                code='MONTHLY_MATERIAL_NOT_FOUND') from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MaterialError('Monthly material resource could not be verified', 422,
                                code='MONTHLY_MATERIAL_INVALID') from exc

    def _project(self, plan):
        value = deepcopy(plan)
        value['plan_id'], value['plan_digest'] = value.pop('id'), value.pop('digest')
        value['material']['changes'] = [{'path': c['path'], 'before_json': json_text(c['before']),
            'after_json': json_text(c['after'])} for c in value['material']['changes']]
        public = self.public.material(value)
        public['public_projection_redacted'] = public != value
        public['public_view_digest'] = '0' * 64
        _require(len(json_text(public).encode()) <= MAX_PUBLIC_BYTES, 'Monthly plan public view exceeds its bound',
                 'MONTHLY_PUBLIC_LIMIT', 413)
        normalized = IndustryMomMaterialPlan.model_validate(public).model_dump()
        normalized['public_view_digest'] = digest({k: v for k, v in normalized.items() if k != 'public_view_digest'})
        return normalized

    def preview(self, request):
        body = IndustryMomMaterialPreview.model_validate(request)
        return self._call(self._project, self._call(self.bridge.preview, review=body.review.model_dump(),
            source_id=body.source_id, source_digest=body.source_digest))

    def _plan(self, plan_id, expected_plan_digest):
        _require(isinstance(plan_id, str) and PLAN_RE.fullmatch(plan_id), 'Invalid monthly plan ID',
                 'MONTHLY_PLAN_INVALID', 422)
        plan = self._call(self.bridge.get_plan, plan_id)
        _require(plan['digest'] == expected_plan_digest, 'Monthly plan target changed; use the exact frozen preview',
                 'MONTHLY_PLAN_STALE')
        return plan

    def get(self, plan_id, expected_plan_digest):
        return self._call(self._project, self._plan(plan_id, expected_plan_digest))

    def _ids(self):
        folder = _safe(self.store.root / 'industry-mom' / 'material-plans')
        if not folder.exists():
            return []
        _require(folder.is_dir(), 'Monthly plan directory is invalid')
        values = []
        with os.scandir(folder) as entries:
            for number, entry in enumerate(entries, 1):
                _require(number <= MAX_PLANS, 'Monthly plan directory exceeds its bounded inventory', 'MONTHLY_PLAN_LIMIT')
                _require(PLAN_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False),
                         'Monthly plan inventory contains an invalid entry')
                values.append(entry.name)
        return sorted(values)

    def _state(self, plan_id):
        folder = self.bridge._folder(plan_id)
        if (folder / 'experiment-link.json').exists() or (folder / 'experiment-link.json').is_symlink():
            view = self._call(self.bridge.lineage, plan_id)
            state = {k: view[k] for k in ('experiment_id', 'status', 'attempt_id', 'result_digest',
                                         'report_sha256', 'verification', 'error')}
            return self.public.material(state)
        # An interrupted acknowledged handoff can exist before link publication.
        # GET reports that state but never reconstructs or queues its sidecar.
        intent = folder / 'experiment-intent.json'
        status = 'not_started'
        if intent.exists() or intent.is_symlink():
            value = self._call(_read, intent)
            plan = self._call(self.bridge.get_plan, plan_id)
            expected = {'schema_version': 1, 'plan_id': plan_id, 'plan_digest': plan['digest'],
                'source_id': plan['industry_source']['id'], 'source_digest': plan['industry_source']['digest'],
                'idempotency_key': 'monthly-material-' + plan['digest']}
            _require(value == expected, 'Monthly handoff intent differs from its frozen plan')
            status = 'handoff_pending'
        return {'experiment_id': None, 'status': status, 'attempt_id': None, 'result_digest': None,
                'report_sha256': None, 'verification': None, 'error': None}

    def list(self, limit=20, offset=0):
        _require(type(limit) is int and 1 <= limit <= 20 and type(offset) is int and 0 <= offset <= 255,
                 'Monthly plan pagination is invalid', 'MONTHLY_PLAN_PAGE_INVALID', 422)
        ids = self._call(self._ids)
        items = []
        for identity in ids[offset:offset + limit]:
            plan = self._call(self.bridge.get_plan, identity)
            state = self._call(self._state, identity)
            items.append({'plan_id': identity, 'plan_digest': plan['digest'],
                'material_id': plan['material']['id'], 'version_id': plan['material']['version_id'],
                'hypothesis_id': plan['candidate']['hypothesis_id'], 'source_id': plan['industry_source']['id'],
                'reviewer_id': self.public.text(plan['review']['reviewer_id']), 'review_source': plan['review']['review_source'],
                'experiment_id': state['experiment_id'], 'status': state['status']})
        return IndustryMomMaterialPlanPage.model_validate({'items': items, 'total': len(ids),
            'limit': limit, 'offset': offset}).model_dump()

    @contextmanager
    def _receipt_lock(self):
        folder = _safe(self.store.root / 'industry-mom' / 'material-api-receipts')
        folder.mkdir(parents=True, exist_ok=True)
        _safe(folder)
        path = folder / '.lock'
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Monthly API receipt lock is invalid')
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _require(time.monotonic() < deadline, 'Another monthly request is still being saved; replay the original key',
                             'MONTHLY_REQUEST_BUSY', 503)
                    time.sleep(0.02)
            current = _safe(path).lstat()
            _require((info.st_dev, info.st_ino) == (current.st_dev, current.st_ino), 'Monthly API receipt lock changed')
            # One lock plus a bounded number of immutable receipt directories.
            with os.scandir(folder) as entries:
                for number, entry in enumerate(entries, 1):
                    _require(number <= MAX_RECEIPTS + 1, 'Monthly API receipt inventory exceeds its bound', 'MONTHLY_RECEIPT_LIMIT')
                    _require(entry.name == '.lock' or (re.fullmatch(r'[a-f0-9]{64}', entry.name)
                        and entry.is_dir(follow_symlinks=False)), 'Monthly API receipt inventory is invalid')
            yield folder
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _mutate(self, operation, request, effect):
        identity = digest({'operation': operation, 'idempotency_key': request['idempotency_key']})
        intent = {'schema_version': 1, 'operation': operation, 'request': request,
                  'request_digest': digest(request)}
        with self._receipt_lock() as root:
            folder = root / identity
            intent_path, receipt_path = folder / 'intent.json', folder / 'receipt.json'
            replayed = intent_path.exists() or intent_path.is_symlink()
            if replayed:
                _require(_read(intent_path) == intent, 'Monthly API key belongs to another request', 'IDEMPOTENCY_CONFLICT')
            else:
                # Admission is checked while the single receipt lock is held.
                # Already-reserved directories can finish their original request.
                if not folder.exists():
                    with os.scandir(root) as entries:
                        occupied = sum(entry.name != '.lock' for entry in entries)
                    _require(occupied < MAX_RECEIPTS, 'Monthly API receipt inventory has reached its bound',
                             'MONTHLY_RECEIPT_LIMIT')
                _put(intent_path, intent)
            saved = None
            if receipt_path.exists() or receipt_path.is_symlink():
                saved = _read(receipt_path)
                _require(isinstance(saved, dict) and set(saved) == {'schema_version', 'operation', 'request_digest', 'effect', 'receipt_digest'}
                    and saved['schema_version'] == 1 and saved['operation'] == operation
                    and saved['request_digest'] == intent['request_digest']
                    and saved['receipt_digest'] == digest({k: v for k, v in saved.items() if k != 'receipt_digest'}),
                    'Monthly API receipt differs from its immutable intent')
            output = effect(saved['effect'] if saved else None)
            body = {'schema_version': 1, 'operation': operation, 'request_digest': intent['request_digest'], 'effect': output}
            receipt = {**body, 'receipt_digest': digest(body)}
            if saved is not None:
                _require(saved == receipt, 'Monthly API receipt points to another effect')
            else:
                _put(receipt_path, receipt)
            return output, replayed

    def create(self, request):
        body = IndustryMomMaterialCreate.model_validate(request)
        frozen = self._call(self.bridge.preview, review=body.review.model_dump(),
            source_id=body.source_id, source_digest=body.source_digest)
        _require((frozen['id'], frozen['digest']) == (body.expected_plan_id, body.expected_plan_digest),
                 'Monthly plan differs from the preview you confirmed', 'MONTHLY_PLAN_STALE')
        def effect(saved):
            if saved is None:
                # Distinct concurrent create keys share this receipt lock.
                # Existing content-addressed plans remain replayable at capacity.
                ids = self._ids()
                _require(frozen['id'] in ids or len(ids) < MAX_PLANS,
                         'Monthly plan inventory has reached its bound', 'MONTHLY_PLAN_LIMIT')
                plan = self.bridge.create_plan(review=body.review.model_dump(), source_id=body.source_id,
                                               source_digest=body.source_digest)
            else:
                _require(saved == {'plan_id': frozen['id'], 'plan_digest': frozen['digest']}, 'Monthly create receipt effect differs')
                plan = self._plan(saved['plan_id'], saved['plan_digest'])
            _require(plan == frozen, 'Monthly plan changed during creation')
            return {'plan_id': plan['id'], 'plan_digest': plan['digest']}
        output, replayed = self._call(self._mutate, 'plan.create', body.model_dump(), effect)
        return IndustryMomMaterialPlanCreated.model_validate({'plan': self.get(output['plan_id'], output['plan_digest']),
            'replayed': replayed}).model_dump()

    def start(self, plan_id, request):
        body = IndustryMomMaterialStart.model_validate(request)
        _require(body.expected_plan_id == plan_id, 'Monthly start route and frozen plan differ', 'MONTHLY_PLAN_STALE')
        plan = self._plan(plan_id, body.expected_plan_digest)
        def effect(saved):
            if saved is None:
                link = self.bridge.create_experiment(plan_id)
            else:
                _require(isinstance(saved, dict) and set(saved) == {'plan_id', 'plan_digest', 'experiment_id'}
                    and saved['plan_id'] == plan_id and saved['plan_digest'] == plan['digest'], 'Monthly start receipt effect differs')
                lineage = self.bridge.lineage(plan_id)
                _require(lineage['experiment_id'] == saved['experiment_id'], 'Monthly receipt refers to another child')
                link = saved
            return {'plan_id': plan_id, 'plan_digest': plan['digest'], 'experiment_id': link['experiment_id']}
        output, replayed = self._call(self._mutate, 'plan.start', body.model_dump(), effect)
        return IndustryMomMaterialStarted.model_validate({**output, 'replayed': replayed}).model_dump()

    def lineage(self, plan_id, expected_plan_digest, expected_attempt_id=None, expected_result_digest=None):
        plan = self._plan(plan_id, expected_plan_digest)
        state = self._call(self._state, plan_id)
        _require((expected_attempt_id is None) == (expected_result_digest is None),
                 'Exact monthly review requires both attempt and result identities', 'MONTHLY_TARGET_INVALID', 422)
        if expected_attempt_id is not None:
            _require(state['status'] == 'completed' and state['attempt_id'] == expected_attempt_id
                     and state['result_digest'] == expected_result_digest, 'Monthly result target changed; refresh the exact attempt',
                     'MONTHLY_RESULT_STALE')
        return IndustryMomMaterialLineage.model_validate({'plan': self._call(self._project, plan), 'lineage': state}).model_dump()

    def by_experiment(self, experiment_id, expected_attempt_id=None, expected_result_digest=None):
        _require(isinstance(experiment_id, str) and re.fullmatch(r'[a-f0-9-]{36}', experiment_id),
                 'Invalid monthly experiment identity', 'MONTHLY_EXPERIMENT_INVALID', 422)
        detail = self._call(self.bridge.experiments.get, experiment_id)
        _require((expected_attempt_id is None) == (expected_result_digest is None),
                 'Exact monthly review requires both attempt and result identities', 'MONTHLY_TARGET_INVALID', 422)
        target = detail['review_target']
        if target is not None:
            _require(expected_attempt_id is not None, 'Completed monthly lineage requires its exact attempt and result',
                     'MONTHLY_TARGET_REQUIRED', 422)
            _require(target == {'attempt_id': expected_attempt_id, 'result_digest': expected_result_digest},
                     'Monthly result target changed; refresh the exact attempt', 'MONTHLY_RESULT_STALE')
        else:
            _require(expected_attempt_id is None, 'Monthly experiment has no current verified result', 'MONTHLY_RESULT_STALE')
        items = []
        for identity in self._call(self._ids):
            link = self.bridge._folder(identity) / 'experiment-link.json'
            if not link.exists() and not link.is_symlink():
                continue
            value = self._call(_read, link)
            expected_keys = {'schema_version', 'plan_id', 'plan_digest', 'source_id', 'source_digest', 'idempotency_key', 'experiment_id'}
            plan_digest = identity.removeprefix('industry_mom_plan_')
            _require(isinstance(value, dict) and set(value) == expected_keys and value['schema_version'] == 1
                and value['plan_id'] == identity and value['plan_digest'] == plan_digest
                and value['idempotency_key'] == 'monthly-material-' + plan_digest, 'Monthly handoff link is invalid')
            child = self._call(self.bridge.experiments._fetch, value['experiment_id'])
            _require(child['idempotency_key'] == value['idempotency_key']
                and child['source_id'] == value['source_id'] and child['source_digest'] == value['source_digest'],
                'Monthly handoff link differs from its original queue effect')
            if value['experiment_id'] != experiment_id:
                continue
            _require(len(items) < 8, 'Monthly experiment material links exceed their bound', 'MONTHLY_LINK_LIMIT')
            plan = self._call(self.bridge.get_plan, identity)
            items.append(self.lineage(identity, plan['digest'], expected_attempt_id, expected_result_digest))
        value = {'experiment_id': experiment_id, 'linked': bool(items), 'items': items}
        _require(len(json_text(value).encode()) <= 8 * MAX_PUBLIC_BYTES, 'Monthly link public view exceeds its bound',
                 'MONTHLY_PUBLIC_LIMIT', 413)
        return IndustryMomExperimentMaterialLineage.model_validate(value).model_dump()


def create_industry_mom_material_router(store):
    service = IndustryMomMaterialPlans(store)
    router = APIRouter(tags=['industry-mom-material-plans'], responses={status: {'model': ErrorResponse}
        for status in (400, 403, 404, 409, 413, 422, 500, 503)})
    prefix = '/api/industry-mom-material-plans'

    @router.post(prefix + '/preview', response_model=IndustryMomMaterialPlan)
    def preview(body: IndustryMomMaterialPreview):
        return service.preview(body)

    @router.post(prefix, response_model=IndustryMomMaterialPlanCreated, status_code=201)
    def create(body: IndustryMomMaterialCreate):
        return service.create(body)

    @router.get(prefix, response_model=IndustryMomMaterialPlanPage)
    def list_plans(limit: int = Query(20, ge=1, le=20), offset: int = Query(0, ge=0, le=255)):
        return service.list(limit, offset)

    @router.get(prefix + '/{plan_id}', response_model=IndustryMomMaterialPlan)
    def get_plan(plan_id: str, expected_plan_digest: str = Query(..., pattern=SHA)):
        return service.get(plan_id, expected_plan_digest)

    @router.post(prefix + '/{plan_id}/start', response_model=IndustryMomMaterialStarted, status_code=202)
    def start(plan_id: str, body: IndustryMomMaterialStart):
        return service.start(plan_id, body)

    @router.get(prefix + '/{plan_id}/lineage', response_model=IndustryMomMaterialLineage)
    def lineage(plan_id: str, expected_plan_digest: str = Query(..., pattern=SHA),
            expected_attempt_id: str | None = Query(None, min_length=1, max_length=128),
            expected_result_digest: str | None = Query(None, pattern=SHA)):
        return service.lineage(plan_id, expected_plan_digest, expected_attempt_id, expected_result_digest)

    @router.get('/api/industry-mom-experiments/{experiment_id}/material-lineage', response_model=IndustryMomExperimentMaterialLineage)
    def by_experiment(experiment_id: str, expected_attempt_id: str | None = Query(None, min_length=1, max_length=128),
            expected_result_digest: str | None = Query(None, pattern=SHA)):
        return service.by_experiment(experiment_id, expected_attempt_id, expected_result_digest)

    return router
