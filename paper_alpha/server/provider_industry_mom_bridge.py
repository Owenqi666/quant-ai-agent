"""Narrow host-owned bridge from reviewed model material to fixed monthly MOM.

This does not add a model operation, daily DSL alias, HTTP mutation or automatic
semantic approval. A host reviewer explicitly binds an adopted hypothesis to the
already verified industry demonstration. Immutable sidecars preserve that extra
provenance; the original industry queue/worker/oracle remains authoritative.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from typing import Literal

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from ..evidence import normalize
from ..storage import digest, json_text
from .. import mom_only, mom_only_workflow
from .db import connect
from .industry_mom import IndustryMomSources, IndustryMomExperiments
from .paper_materials import PaperMaterials
from .paper_segments import MaterialError, verify_current_selection
from .provider_material_bridge import accepted_packet_resolver

BRIDGE_VERSION = 'reviewed-material-fixed-industry-mom-v1'
MAX_BYTES = 262144


class MonthlyMomReview(AuthorModel):
    """Host-written semantic decision; citations alone do not create this."""
    schema_version: Literal[1]
    review_source: Literal['codex_proxy', 'user']
    reviewer_id: str = Field(min_length=1, max_length=100)
    decision: Literal['approved_for_fixed_industry_demo']
    material_id: str = Field(pattern=r'^material_[a-f0-9]{64}$')
    version_id: str = Field(pattern=r'^material_version_[a-f0-9]{64}$')
    material_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    hypothesis_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    citation_accuracy_checked: Literal[True]
    hypothesis_fidelity_checked: Literal[True]
    monthly_return_semantics_checked: Literal[True]
    scope_modifications_checked: Literal[True]
    no_unsupported_field_substitution: Literal[True]
    window_months: Literal[11]
    skip_months: Literal[1]
    missing_policy: Literal['complete']
    fill_policy: Literal['none']
    rationale: str = Field(min_length=1, max_length=4000)

    @model_validator(mode='before')
    @classmethod
    def literal_types(cls, value):
        if isinstance(value, dict):
            for name in ('window_months', 'skip_months'):
                if type(value.get(name)) is not int:
                    raise ValueError(name + ' must be an integer')
            for name in ('citation_accuracy_checked', 'hypothesis_fidelity_checked',
                         'monthly_return_semantics_checked', 'scope_modifications_checked',
                         'no_unsupported_field_substitution'):
                if value.get(name) is not True:
                    raise ValueError(name + ' requires an explicit true review')
        return value

    @model_validator(mode='after')
    def bounded(self):
        if not self.rationale.strip() or not self.reviewer_id.strip():
            raise ValueError('A named reviewer and rationale are required')
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError('Review evidence IDs must be unique')
        return self


def _require(condition, message, code='MONTHLY_MATERIAL_INTEGRITY'):
    if not condition:
        raise MaterialError(message, code=code)


def _safe(path):
    path = Path(path).absolute()
    _require(not any(p.is_symlink() for p in (path, *path.parents)), 'Bridge paths may not contain symlinks')
    return path


def _read(path):
    path = _safe(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and 0 < before.st_size <= MAX_BYTES, 'Bridge artifact must be a bounded single-linked regular file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(descriptor)
        current = path.lstat()
        _require(len(raw) <= MAX_BYTES and (before.st_ino, before.st_size, before.st_mtime_ns) ==
                 (after.st_ino, after.st_size, after.st_mtime_ns) and
                 (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) ==
                 (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), 'Bridge artifact changed while reading')
    finally:
        os.close(descriptor)
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, 'Duplicate JSON key in bridge artifact')
            result[key] = value
        return result
    def reject(value):
        raise MaterialError('Invalid bridge JSON constant ' + value)
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    _require(json_text(value).encode('utf8') == raw, 'Bridge artifact is not the exact canonical publication')
    return value


def _rename_exclusive(directory_fd, source_name, target_name):
    """Single-link, atomic publication without replacing any existing name.

    A link/unlink publication would leave a two-link final file if the process
    stopped between those steps, contradicting the reader's single-link fence.
    Both supported POSIX runtimes provide an exclusive rename for this purpose.
    """
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = getattr(library, 'renameatx_np', None)
        flags = 0x00000004  # RENAME_EXCL, sys/stdio.h
    elif sys.platform.startswith('linux'):
        rename = getattr(library, 'renameat2', None)
        flags = 1  # RENAME_NOREPLACE
    else:
        rename = None
    _require(rename is not None, 'Exclusive atomic publication is unavailable on this runtime')
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(directory_fd, os.fsencode(source_name), directory_fd, os.fsencode(target_name), flags):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target_name)


def _put(path, value):
    path = _safe(path)
    raw = json_text(value).encode('utf8')
    _require(0 < len(raw) <= MAX_BYTES, 'Bridge artifact exceeds its byte bound')
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path.parent)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stage_name = None
    stage_identity = None
    try:
        parent = os.fstat(directory_fd)
        _require(stat.S_ISDIR(parent.st_mode), 'Bridge publication parent is not a directory')
        def parent_unchanged():
            current = _safe(path.parent).lstat()
            _require((current.st_dev, current.st_ino) == (parent.st_dev, parent.st_ino),
                     'Bridge publication directory changed')
        parent_unchanged()
        if path.exists():
            _require(_read(path) == value, 'Immutable bridge artifact differs; use a new plan')
            os.fsync(directory_fd)
            return
        # An interrupted private stage never occupies the immutable final name.
        # Unique names allow later retries without deleting unknown old files.
        stage_name = '.' + path.name + '.' + uuid.uuid4().hex + '.pending'
        descriptor = os.open(stage_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        stage_identity = (before.st_dev, before.st_ino)
        with os.fdopen(descriptor, 'wb') as stream:
            _require(stream.write(raw) == len(raw), 'Incomplete bridge staging write')
            stream.flush()
            os.fsync(stream.fileno())
            written = os.fstat(stream.fileno())
            _require(stat.S_ISREG(written.st_mode) and written.st_nlink == 1
                     and written.st_size == len(raw), 'Bridge stage is not a complete single-linked file')
        parent_unchanged()
        stage = os.stat(stage_name, dir_fd=directory_fd, follow_symlinks=False)
        _require((stage.st_dev, stage.st_ino) == stage_identity and stat.S_ISREG(stage.st_mode)
                 and stage.st_nlink == 1 and stage.st_size == len(raw), 'Bridge stage changed before publication')
        try:
            _rename_exclusive(directory_fd, stage_name, path.name)
            stage_name = None
        except FileExistsError:
            # Another writer may have won. Its complete canonical bytes must
            # match; malformed or different final artifacts remain untouched.
            _require(_read(path) == value, 'Immutable bridge artifact differs; use a new plan')
        parent_unchanged()
        os.fsync(directory_fd)
        _require(_read(path) == value, 'Bridge artifact changed after publication')
    finally:
        try:
            if stage_name is not None:
                try:
                    leftover = os.stat(stage_name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    # Clean only the stage this writer created, never an
                    # unrelated/replaced file or a crash remnant from a peer.
                    if (leftover.st_dev, leftover.st_ino) == stage_identity:
                        os.unlink(stage_name, dir_fd=directory_fd)
                        os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _covers(evidence, anchor):
    cursor, end = anchor['normalized_start'], anchor['normalized_end_exclusive']
    for item in sorted((e for e in evidence if e['page'] == anchor['page_1_based']),
                       key=lambda e: e['normalized_start']):
        if item['normalized_end'] <= cursor:
            continue
        if item['normalized_start'] > cursor:
            return False
        cursor = max(cursor, item['normalized_end'])
        if cursor >= end:
            return True
    return False


class IndustryMomMaterialBridge:
    def __init__(self, store):
        self.store = store
        self.materials = PaperMaterials(store, accepted_packet_resolver=accepted_packet_resolver(store))
        self.sources = IndustryMomSources(store)
        self.experiments = IndustryMomExperiments(store)

    def _folder(self, plan_id):
        _require(isinstance(plan_id, str) and re.fullmatch(r'industry_mom_plan_[a-f0-9]{64}', plan_id),
                 'Invalid material MOM plan identity')
        return _safe(self.store.root / 'industry-mom' / 'material-plans' / plan_id)

    def preview(self, *, review, source_id, source_digest):
        """Read-only semantic decision + raw-ledger + source verification."""
        review = MonthlyMomReview.model_validate(review).model_dump()
        version = self.materials.get_version(review['material_id'], review['version_id'])
        _require(version['digest'] == review['material_digest'], 'Review binds another adopted material version')
        verify_current_selection(self.store, version['selection'])
        draft = self.materials.get(review['material_id'])
        with closing(connect(self.store.db_path)) as connection:
            connection.execute('BEGIN')
            self.materials._accepted_packet(connection, version['source'], digest(draft['draft']))
            from .proposal_requests import ProposalRequests
            proposal = ProposalRequests(self.store, configs={})._load(connection, version['source']['proposal_id'])
            attempt = next(a for a in proposal['attempts'] if a['id'] == version['source']['attempt_id'])
            provider_origin = {'proposal_id': proposal['id'], 'attempt_id': attempt['id'],
                'accepted_response_digest': proposal['accepted_response_digest'],
                'context_digest': proposal['context_digest'], 'raw_response_sha256': attempt['raw_sha256'],
                'receipt_digest': digest(attempt['receipt']), 'fixture': attempt['receipt']['fixture'],
                'model': proposal['config']['model'], 'config_digest': digest(proposal['config'])}
        _require(version['actor'] == 'user', 'Only explicitly adopted material can seed a monthly plan')
        matches = [h for h in version['material']['hypotheses'] if h['id'] == review['hypothesis_id']]
        _require(len(matches) == 1, 'Reviewed hypothesis is not part of the adopted material')
        hypothesis = matches[0]
        _require(not hypothesis['unresolved_rules'] and not version['material']['unresolved_rules'],
                 'Resolve selected material rules before a monthly experiment', 'MONTHLY_MATERIAL_UNRESOLVED')
        _require(set(review['evidence_ids']) <= set(hypothesis['evidence_ids']),
                 'Review cites evidence outside its selected hypothesis')
        evidence = [e for e in version['material']['evidence'] if e['id'] in review['evidence_ids']]
        _require(len(evidence) == len(review['evidence_ids']), 'Review evidence is missing')
        source = self.sources.get(source_id)
        _require(source['digest'] == source_digest, 'Industry source digest changed')
        config, method = json.loads(source['config_json']), json.loads(source['method_json'])
        mom_only.validate_config(config)
        mom_only_workflow._validate_method(method, config, source['archive_sha256'])
        _require(version['source']['paper_sha256'] == method['paper']['document_sha256'],
                 'Only the fixed reviewed momentum paper can seed this industry method', 'MONTHLY_PAPER_UNSUPPORTED')
        anchors = [a for a in method['evidence'] if a['id'] in method['paper_signal']['window_evidence_ids']]
        covered = [a for a in anchors if _covers(evidence, a)]
        _require(covered, 'Reviewed citations do not cover the fixed 11-month skip-1 paper window',
                 'MONTHLY_WINDOW_EVIDENCE_MISSING')
        # Matching the original page bytes above is independent of reviewer
        # judgement. The unchanged method artifact supplies known window facts;
        # no formula/window is guessed from the model's prose.
        for anchor in covered:
            chunks = [c for c in version['selection']['chunks'] if c['page'] == anchor['page_1_based']]
            text = ''.join(c['text'] for c in chunks)
            _require(text[anchor['normalized_start']:anchor['normalized_end_exclusive']] == normalize(anchor['quote']),
                     'Pinned monthly window anchor differs from current selected source')
        body = {'schema_version': 1, 'bridge_version': BRIDGE_VERSION,
            'construction': 'host_owned_known_monthly_method_from_explicitly_reviewed_model_material',
            'candidate': {'id': 'fixed_industry_mom_11_skip1', 'hypothesis_id': hypothesis['id'],
                'origin': 'user_modification', 'expression_kind': 'structured_monthly_plan',
                'signal': deepcopy(config['signal']), 'formation_offsets': list(range(-12, -1)),
                'frequency': 'calendar_month', 'return_semantics': 'monthly_total_return_decimal',
                'changes': ['Original international single stocks replaced by 49 provider industry portfolios.',
                    'Original country-neutralization/stock weights replaced by the fixed industry tercile method.',
                    'Development dates, missing rules, same-sample baseline and gross-only cost scope are project choices.']},
            'review': review, 'review_digest': digest(review),
            'material': {'id': version['material_id'], 'version_id': version['id'],
                'version_digest': version['digest'], 'draft_digest': version['draft_digest'],
                'source': deepcopy(version['source']), 'provider_origin': provider_origin,
                'hypothesis': deepcopy(hypothesis),
                'evidence': deepcopy(evidence), 'changes': deepcopy(version['changes']),
                'semantic_fidelity': 'unverified'},
            'window_anchors': deepcopy(covered),
            'industry_source': {k: source[k] for k in ('id', 'digest', 'archive_sha256', 'method_digest',
                'config_digest', 'input_digest', 'panel_digest', 'manifest_digest')},
            'config': config, 'method_digest': method['contract_digest'],
            'reserved_evaluated': False, 'original_single_stock_plan_unlocked': False,
            'model_generated_monthly_dsl': False, 'human_satisfaction_measured': False}
        return {**body, 'id': 'industry_mom_plan_' + digest(body), 'digest': digest(body)}

    def create_plan(self, *, review, source_id, source_digest):
        plan = self.preview(review=review, source_id=source_id, source_digest=source_digest)
        _put(self._folder(plan['id']) / 'plan.json', plan)
        return self.get_plan(plan['id'])

    def get_plan(self, plan_id):
        value = _read(self._folder(plan_id) / 'plan.json')
        _require(isinstance(value, dict) and value.get('id') == plan_id
                 and value.get('digest') == digest({k: v for k, v in value.items() if k not in {'id', 'digest'}}),
                 'Immutable material MOM plan identity differs')
        expected = self.preview(review=value['review'], source_id=value['industry_source']['id'],
                                source_digest=value['industry_source']['digest'])
        _require(value == expected, 'Material MOM plan differs from reviewed source and adopted material')
        return value

    def create_experiment(self, plan_id):
        plan = self.get_plan(plan_id)
        key = 'monthly-material-' + plan['digest']
        intent = {'schema_version': 1, 'plan_id': plan_id, 'plan_digest': plan['digest'],
                  'source_id': plan['industry_source']['id'], 'source_digest': plan['industry_source']['digest'],
                  'idempotency_key': key}
        folder = self._folder(plan_id)
        _put(folder / 'experiment-intent.json', intent)
        try:
            ack = self.experiments.create(intent['source_id'], intent['source_digest'], key)
            detail = self.experiments.get(ack['experiment_id'])
            _require(detail['experiment']['source_id'] == intent['source_id']
                     and detail['experiment']['source_digest'] == intent['source_digest'], 'Monthly child belongs to another source')
            link = {**intent, 'experiment_id': ack['experiment_id']}
            _put(folder / 'experiment-link.json', link)
            _require(self.get_plan(plan_id) == plan, 'Plan source changed during queue handoff')
            return link
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failed = {**intent, 'status': 'handoff_failed', 'error': self.store._safe_error(str(exc))}
            _put(folder / ('failure-' + digest(failed) + '.json'), failed)
            raise

    def record_outcome(self, plan_id):
        """Append one immutable observation; never mark an unfinished job passed."""
        view = self.lineage(plan_id)
        snapshot = {k: view[k] for k in ('schema_version', 'plan_id', 'plan_digest', 'experiment_id',
            'status', 'attempt_id', 'result_digest', 'report_sha256', 'verification', 'error')}
        _put(self._folder(plan_id) / ('outcome-' + digest(snapshot) + '.json'), snapshot)
        return snapshot

    def lineage(self, plan_id):
        plan = self.get_plan(plan_id)
        folder = self._folder(plan_id)
        intent, link = _read(folder / 'experiment-intent.json'), _read(folder / 'experiment-link.json')
        expected = {'schema_version': 1, 'plan_id': plan_id, 'plan_digest': plan['digest'],
                    'source_id': plan['industry_source']['id'], 'source_digest': plan['industry_source']['digest'],
                    'idempotency_key': 'monthly-material-' + plan['digest']}
        _require(intent == expected and isinstance(link, dict) and set(link) == set(expected) | {'experiment_id'}
                 and {k: link[k] for k in expected} == expected, 'Monthly experiment sidecar handoff differs')
        detail = self.experiments.get(link['experiment_id'])
        row = self.experiments._fetch(link['experiment_id'])
        _require(row['idempotency_key'] == expected['idempotency_key']
                 and row['source_id'] == expected['source_id'] and row['source_digest'] == expected['source_digest']
                 and row['config_digest'] == digest(plan['config']), 'Monthly child is not the exact plan-owned queue effect')
        verification = detail['verification']
        if detail['experiment']['status'] == 'completed':
            _require(verification is not None and verification['reference_passed'] is True
                     and verification['reserved_evaluated'] is False
                     and verification['config_digest'] == plan['industry_source']['config_digest']
                     and verification['method_digest'] == plan['industry_source']['method_digest']
                     and verification['panel_digest'] == plan['industry_source']['panel_digest'],
                     'Monthly completed result differs from reviewed plan')
        result = {'schema_version': 1, 'bridge_version': BRIDGE_VERSION, 'plan_id': plan_id,
            'plan_digest': plan['digest'], 'experiment_id': link['experiment_id'],
            'status': detail['experiment']['status'], 'attempt_id': detail['experiment']['attempt_id'],
            'result_digest': verification['result_digest'] if verification else None,
            'report_sha256': hashlib.sha256(detail['report_markdown'].encode()).hexdigest() if detail['report_markdown'] else None,
            'verification': verification, 'error': detail['experiment']['error'], 'plan': plan,
            'report_markdown': detail['report_markdown'], 'human_satisfaction_measured': False}
        # Saved observations may be historical queued/failed states. Validate
        # their immutable identity without replacing them with the latest state.
        snapshots = sorted(folder.glob('outcome-*.json'))
        _require(len(snapshots) <= 256, 'Monthly observation history exceeds its bound')
        for path in snapshots:
            saved = _read(path)
            _require(path.name == 'outcome-' + digest(saved) + '.json'
                     and isinstance(saved, dict) and saved.get('plan_id') == plan_id
                     and saved.get('plan_digest') == plan['digest']
                     and saved.get('experiment_id') == link['experiment_id'], 'Saved monthly observation was changed')
            _require(set(saved) == {'schema_version', 'plan_id', 'plan_digest', 'experiment_id', 'status',
                'attempt_id', 'result_digest', 'report_sha256', 'verification', 'error'} and saved['schema_version'] == 1,
                'Saved monthly observation fields differ')
            _require(saved['status'] in {'queued', 'running', 'cancelling', 'completed', 'failed', 'cancelled', 'interrupted'},
                'Saved monthly observation has an invalid status')
            if saved['status'] == 'completed':
                _require(saved['attempt_id'] == result['attempt_id'] and saved['result_digest'] == result['result_digest']
                    and saved['verification'] == result['verification'] and saved['report_sha256'] == result['report_sha256']
                    and result['status'] == 'completed' and saved['error'] is None,
                    'Saved completed monthly observation differs from its verified result')
            else:
                _require(all(saved[k] is None for k in ('result_digest', 'report_sha256', 'verification')),
                    'Unfinished monthly observation contains fabricated computed results')
                if saved['attempt_id'] is not None:
                    _require(any(a['id'] == saved['attempt_id'] for a in detail['attempts']),
                        'Saved monthly observation cites an unknown attempt')
        failures = sorted(folder.glob('failure-*.json'))
        _require(len(failures) <= 256, 'Monthly failure history exceeds its bound')
        for path in failures:
            saved = _read(path)
            _require(isinstance(saved, dict) and set(saved) == set(expected) | {'status', 'error'}
                     and path.name == 'failure-' + digest(saved) + '.json'
                     and {k: saved[k] for k in expected} == expected
                     and saved['status'] == 'handoff_failed' and isinstance(saved['error'], str),
                     'Saved monthly handoff failure was changed')
        return result
