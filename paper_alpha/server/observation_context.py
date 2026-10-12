"""Bounded, read-only selectors for the observation form.

The existing immutable observation validator remains authoritative. Context
only supplies server-owned identities and verified outputs; it never creates
a human observation, chooses a reviewer, or derives human elapsed time.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import tomllib

from .db import connect
from .observation_context_schema import ObservationContext, ContextReview
from .research_assessments import validate_assessment
from .service import REPO, ServiceError, loads
from .workflow_observations import WorkflowObservations, _protocol
from ..storage import digest, json_text, read_json
from ..source_identity import portable_source_commit
from ..workflow import code_files, environment

PAGE_SIZE = 20
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_TREE_FILES = 3000
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ROW_BYTES = 1024 * 1024
LIMITATIONS = [
    '本接口仅提供只读上下文，不创建观测，也不默认声明人工来源或任务完成。',
    '运行、尝试与审核必须由本人选择；历史运行不证明本次人工工作已经发生。',
    '审核维度可明确引用，审核时间不会自动加入本次计时；必须确认属于同一次实际会话。',
    '服务端环境只描述当前服务，不认证参与者设备或外部 CLI 环境；两条路径需如实确认相同条件。',
    '当前源码摘要不等同于 Git 提交；没有可靠映射时提交字段为空，需要一次性声明。',
    '发布包的提交映射只核对本机构建声明与文件一致性，不是签名认证或远端 Git 证明。',
    '上下文可能在提交前变化；最终预检和不可变保存仍重新核验精确绑定。',
    '运行、尝试与结构化审核只列前 20 条；审核总数包含未通过指纹或结构校验而未显示的记录。',
]


def _safe_file(path, base, *, maximum=MAX_FILE_BYTES):
    path, base = Path(path).absolute(), Path(base).absolute()
    if base != base.resolve() or '..' in path.parts or not path.is_relative_to(base):
        raise ServiceError('上下文文件不在指定的服务端目录中', 409)
    current = base
    for part in path.relative_to(base).parts:
        current = current / part
        if current.is_symlink():
            raise ServiceError('上下文文件不能使用符号链接', 409)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ServiceError('上下文只读取普通、非共享链接文件', 409)
    if metadata.st_size > maximum:
        raise ServiceError('上下文文件超过读取上限', 413)
    return metadata.st_size


def _safe_json(path, base):
    _safe_file(path, base, maximum=MAX_JSON_BYTES)
    return read_json(path)


def _bounded_tree(base):
    """Reject links and bound files before legacy integrity verification reads."""
    base = Path(base).absolute()
    if base != base.resolve() or not base.is_dir():
        raise ServiceError('实验快照目录不可用或包含符号链接', 409)
    count, total, visited = 0, 0, 0
    for directory, directories, files in os.walk(base, followlinks=False):
        visited += len(directories) + len(files)
        if visited > MAX_TREE_FILES * 3:
            raise ServiceError('实验快照目录条目超过上限', 413)
        for name in directories:
            if (Path(directory) / name).is_symlink():
                raise ServiceError('实验快照目录包含符号链接', 409)
        for name in files:
            count += 1
            total += _safe_file(Path(directory) / name, base)
            if count > MAX_TREE_FILES or total > MAX_TREE_BYTES:
                raise ServiceError('实验快照超过上下文核验的文件或字节上限', 413)


def _safe_snapshot_references(output, manifest, state):
    """Legacy verify_run reads manifest paths; preclude traversal before calling it."""
    mappings = [manifest.get('snapshot_files', {}), state.get('tool_artifacts', {})]
    mappings.extend(candidate.get('artifacts', {}) for candidate in state.get('candidates', []))
    total = 0
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ServiceError('实验快照的文件清单无效', 409)
        total += len(mapping)
        if total > MAX_TREE_FILES:
            raise ServiceError('实验快照的引用数量超过上限', 413)
        for name in mapping:
            path = PurePosixPath(name)
            if not isinstance(name, str) or not name or path.is_absolute() or '..' in path.parts or '\\' in name:
                raise ServiceError('实验快照含有目录外文件引用', 409)
            _safe_file(output / name, output)


def runtime_context():
    """No Git discovery: a containing checkout does not identify an installed bundle."""
    current = environment()
    files = code_files()
    if len(files) > MAX_TREE_FILES:
        raise ServiceError('服务端源码清单超过上限', 413)
    inventory, total = {}, 0
    for relative, path in files.items():
        total += _safe_file(path, REPO)
        if total > MAX_TREE_BYTES:
            raise ServiceError('服务端源码大小超过上限', 413)
        inventory[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    _safe_file(REPO / 'pyproject.toml', REPO, maximum=MAX_ROW_BYTES)
    version = tomllib.loads((REPO / 'pyproject.toml').read_text())['project']['version']
    commit = portable_source_commit(REPO)
    return {'version': version, 'code_commit': commit, 'code_digest': digest(inventory),
            'environment': {'machine': platform.machine() or 'unknown-server-architecture',
                            'os': current['platform'], 'python': current['python'],
                            'dependencies': json_text(current['packages']).strip()},
            'provenance': 'server_runtime', 'code_commit_verified': commit is not None}


class ObservationContextService:
    def __init__(self, store):
        self.store = store
        self.observations = WorkflowObservations(store)

    @staticmethod
    def _id(value):
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 64):
            raise ServiceError('请选择有效的研究、修订、运行或尝试', 422)
        return value

    def _science_bounds(self, connection, research, revision_id):
        for table, identity, fields in (
            ('revisions', revision_id, ['task']),
            ('papers', research['paper_id'], ['document']),
            ('datasets', research['dataset_id'], ['metadata']),
        ):
            sizes = connection.execute('SELECT ' + ','.join(f'length({field})' for field in fields)
                                       + f' FROM {table} WHERE id=?', (identity,)).fetchone()
            if sizes is None:
                raise ServiceError('研究来源不完整', 409)
            if any(size is not None and size > MAX_JSON_BYTES for size in sizes):
                raise ServiceError('研究来源超过上下文读取上限', 413)
        for table, identity, paths in (('papers', research['paper_id'], ['pdf_path']),
                                       ('datasets', research['dataset_id'], ['data_path', 'metadata_path'])):
            row = connection.execute('SELECT ' + ','.join(paths) + f' FROM {table} WHERE id=?', (identity,)).fetchone()
            for path in row:
                _safe_file(path, self.store.root)

    @staticmethod
    def _binding_request(task_sha, revision_id, run_id=None, attempt_id=None):
        outputs = {'task_sha256': task_sha, 'revision_id': revision_id, 'run_id': run_id,
                   'attempt_id': attempt_id, 'review_ids': [], 'external_run_reference': None,
                   'verification_evidence': None, 'report_reference': None}
        return {'condition': 'workbench', 'completion': 'incomplete', 'intervals': [],
                'bound_outputs': outputs, 'dimensions': None}

    def _attempt_snapshot(self, connection, run, attempt):
        # Never use an output_dir supplied by a request or stored alias.
        base = self.store.root / 'runs' / run['id'] / 'attempts' / attempt['id']
        output = base / 'output'
        _bounded_tree(base)
        for column in ('verification',):
            if attempt.get(column) and len(attempt[column].encode()) > MAX_ROW_BYTES:
                raise ServiceError('实验核验记录超过读取上限', 413)
        manifest, state = _safe_json(output / 'manifest.json', output), _safe_json(output / 'state.json', output)
        if not isinstance(manifest, dict) or not isinstance(state, dict):
            raise ServiceError('实验快照的 JSON 结构无效', 409)
        _safe_snapshot_references(output, manifest, state)
        count = connection.execute('SELECT COUNT(*) FROM artifacts WHERE attempt_id=?', (attempt['id'],)).fetchone()[0]
        if count > MAX_TREE_FILES:
            raise ServiceError('实验产物清单超过读取上限', 413)
        for row in connection.execute('SELECT path FROM artifacts WHERE attempt_id=?', (attempt['id'],)):
            _safe_file(row['path'], base / 'exports')
        return manifest, state

    def get_context(self, research_id, *, revision_id=None, run_id=None, attempt_id=None):
        for identity in (research_id, revision_id, run_id, attempt_id):
            self._id(identity)
        if attempt_id is not None and run_id is None:
            raise ServiceError('选择尝试前请先选择运行', 422)
        protocol, protocol_digest = _protocol()
        task_sha = protocol['inputs']['evaluation_suites/v05/task.json']
        result = {'schema_version': 1, 'research_id': research_id,
                  'protocol': {'id': protocol['protocol_id'], 'digest': protocol_digest,
                               'task_sha256': task_sha, 'candidate_id': protocol['candidate_id'],
                               'engine_mode': protocol['engine_mode']},
                  'selected': None, 'limitations': list(LIMITATIONS)}
        try:
            result['runtime'] = runtime_context()
            with closing(connect(self.store.db_path)) as connection:
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                research = self.store._one(connection, 'researches', research_id)
                if revision_id is None and run_id is not None:
                    selected_run = connection.execute('SELECT research_id,revision_id FROM runs WHERE id=?', (run_id,)).fetchone()
                    if selected_run is None:
                        raise ServiceError('所选运行不存在', 404)
                    if selected_run['research_id'] != research_id:
                        raise ServiceError('所选运行不属于当前研究', 422)
                    revision_id = selected_run['revision_id']
                revision_id = revision_id or research['latest_revision_id']
                revision = connection.execute('SELECT research_id FROM revisions WHERE id=?', (revision_id,)).fetchone()
                if revision is None or revision['research_id'] != research_id:
                    raise ServiceError('所选修订不属于当前研究', 422)
                result['revision_id'] = revision_id
                self._science_bounds(connection, research, revision_id)
                request = self._binding_request(task_sha, revision_id)
                try:
                    self.observations._binding(connection, research_id, request, protocol)
                    compatibility = {'compatible': True, 'reason': None}
                except ServiceError as exc:
                    if exc.status != 422:
                        raise
                    compatibility = {'compatible': False, 'reason': '此修订的论文、数据或科学条件与固定的单候选 Alpha101 对照协议不一致。'}
                result['compatibility'] = compatibility
                total = connection.execute('SELECT COUNT(*) FROM runs WHERE research_id=?', (research_id,)).fetchone()[0]
                runs = [dict(row) for row in connection.execute(
                    'SELECT id,revision_id,status,mode,created_at,attempt_count FROM runs WHERE research_id=? '
                    'ORDER BY created_at DESC,id DESC LIMIT ?', (research_id, PAGE_SIZE))]
                result.update(runs=runs, runs_total=total, runs_truncated=total > len(runs))
                if run_id is not None:
                    result['selected'] = self._selection(connection, research_id, revision_id, run_id, attempt_id,
                                                          protocol, task_sha, compatibility)
            return ObservationContext.model_validate(result).model_dump()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ServiceError):
                raise
            raise ServiceError('只读观测上下文不可用，请核对研究与实验快照', 409) from exc

    def _selection(self, connection, research_id, revision_id, run_id, attempt_id, protocol, task_sha, compatibility):
        # Do not fetch potentially large state text just to populate selectors.
        row = connection.execute('SELECT id,research_id,revision_id,status,mode FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ServiceError('所选运行不存在', 404)
        run = dict(row)
        if run['research_id'] != research_id or run['revision_id'] != revision_id:
            raise ServiceError('所选运行不属于当前研究及修订，请重新选择', 422)
        if run['mode'] != protocol['engine_mode']:
            raise ServiceError('流程对照要求使用规范化固定模式', 422)
        if not compatibility['compatible']:
            raise ServiceError(compatibility['reason'], 422)
        sizes = connection.execute('SELECT length(state),length(verification) FROM runs WHERE id=?', (run_id,)).fetchone()
        if any(size is not None and size > MAX_JSON_BYTES for size in sizes):
            raise ServiceError('运行记录超过上下文读取上限', 413)
        total = connection.execute('SELECT COUNT(*) FROM attempts WHERE run_id=?', (run_id,)).fetchone()[0]
        attempts = [dict(row) for row in connection.execute(
            'SELECT id,number,status,started_at,finished_at FROM attempts WHERE run_id=? ORDER BY number DESC LIMIT ?',
            (run_id, PAGE_SIZE))]
        request = self._binding_request(task_sha, revision_id, run_id, attempt_id)
        selected = {'revision_id': revision_id, 'run_id': run_id, 'attempt_id': attempt_id,
                    'status': run['status'], 'verified': False, 'verification_error': None,
                    'bound_outputs': request['bound_outputs'], 'attempts': attempts,
                    'attempts_total': total, 'attempts_truncated': total > len(attempts),
                    'reviews': [], 'reviews_total': 0, 'reviews_truncated': False}
        if attempt_id is None:
            return selected
        size = connection.execute('SELECT length(verification) FROM attempts WHERE id=?', (attempt_id,)).fetchone()
        if size is not None and size[0] is not None and size[0] > MAX_ROW_BYTES:
            raise ServiceError('实验核验记录超过读取上限', 413)
        attempt = self.store._one(connection, 'attempts', attempt_id)
        if attempt['run_id'] != run_id:
            raise ServiceError('所选尝试不属于当前运行', 422)
        selected['status'] = attempt['status']
        if attempt['status'] not in {'completed', 'failed', 'interrupted', 'cancelled'}:
            selected['verification_error'] = '尝试尚未结束，请等待产生不可变结果后再绑定。'
            return selected
        verification = loads(attempt['verification'])
        if verification is not None and not isinstance(verification, dict):
            raise ServiceError('实验核验记录结构无效', 409)
        if not (verification or {}).get('verified'):
            selected['verification_error'] = '此尝试没有已核验产物；可以记录未完成过程，不能声明已完成核验。'
            return selected
        try:
            _, state = self._attempt_snapshot(connection, run, attempt)
            binding = self.observations._binding(connection, research_id, request, protocol)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ServiceError) and exc.status == 413:
                raise
            selected['verification_error'] = '实验快照未通过完整性核验，请检查原始产物后重试。'
            return selected
        selected['verified'] = binding['outputs_verified']
        selected['bound_outputs']['verification_evidence'] = (
            f'服务端已核验运行 {run_id} 的确切尝试 {attempt_id}；不代表人工研究判断已完成。')
        report = connection.execute("SELECT id FROM artifacts WHERE run_id=? AND attempt_id=? AND name='report.md'",
                                    (run_id, attempt_id)).fetchone()
        if report:
            selected['bound_outputs']['report_reference'] = f"/api/runs/{run_id}/artifacts/{report['id']}"
        candidate = next((value for value in state.get('candidates', []) if value['id'] == protocol['candidate_id']), None)
        if candidate is not None:
            self._reviews(connection, selected, candidate, run, attempt)
        return selected

    def _reviews(self, connection, selected, candidate, run, attempt):
        # Only inspect one bounded page. A count does not certify every unreturned
        # review; truncated/invalid entries remain explicit rather than auto-picked.
        clause = 'run_id=? AND revision_id=? AND attempt_id=? AND candidate_id=? AND assessment IS NOT NULL'
        values = (run['id'], run['revision_id'], attempt['id'], candidate['id'])
        total = connection.execute('SELECT COUNT(*) FROM reviews WHERE ' + clause, values).fetchone()[0]
        rows = connection.execute('SELECT id,length(assessment) AS bytes FROM reviews WHERE ' + clause
                                  + ' ORDER BY created_at DESC,id DESC LIMIT ?', (*values, PAGE_SIZE)).fetchall()
        selected.update(reviews_total=total, reviews_truncated=total > len(rows))
        expected_digest = digest(candidate)
        for item in rows:
            if item['bytes'] > MAX_ROW_BYTES:
                raise ServiceError('审核记录超过上下文读取上限', 413)
            review = self.store._one(connection, 'reviews', item['id'])
            if review['result_digest'] != expected_digest:
                continue
            try:
                assessment = validate_assessment(loads(review['assessment']))
                if (assessment['expected_attempt_id'] != attempt['id']
                        or assessment['expected_result_digest'] != expected_digest):
                    continue
                value = ContextReview.model_validate({
                    'id': review['id'], 'candidate_id': review['candidate_id'], 'source': review['source'],
                    'reviewer': assessment['reviewer'], 'created_at': review['created_at'],
                    'dimensions': assessment['dimensions'], 'active_intervals': assessment['active_intervals'],
                    'result_digest': expected_digest}).model_dump()
            except (ValueError, TypeError, KeyError):
                continue
            selected['reviews'].append(deepcopy(value))
