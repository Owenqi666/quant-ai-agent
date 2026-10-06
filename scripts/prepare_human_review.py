"""Prepare a fixed Alpha101 draft review packet against an explicit local workbench.

Only claim preview/create may be posted. No experiment, label, review, reference
set, observation, or application update is performed. Verification is offline
unless --live is explicitly requested. Hashes prove local consistency, not authorship.
"""
import argparse
from contextlib import contextmanager
import fcntl
from hashlib import sha256
from http.client import HTTPException
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from human_review_packet import build_claim_request, build_packet, validate_inputs, verify_packet

SUPPORTED_RUNTIMES = {(16, '0.20.0'), (17, '0.21.0'), (17, '0.22.0')}
MAX_JSON = 8 * 1024 * 1024
MAX_PDF = 16 * 1024 * 1024
MAX_PACKAGE = 32 * 1024 * 1024
INITIAL_FILES = ('inputs.json', 'request.json', 'paper.pdf')
FINAL_FILES = (*INITIAL_FILES, 'preparation.json', 'preview.json', 'claims.json',
               'targets.json', 'statuses.json', 'packet.json', 'report.md')
DATA_FILES = (*FINAL_FILES, 'manifest.json')
ALLOWED_POSTS = {'/api/research-claims/preview', '/api/research-claims'}


class PreparationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return sha256(canonical(value)).hexdigest()


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreparationError('Duplicate JSON key')
        result[key] = value
    return result


def decode(data):
    if len(data) > MAX_JSON:
        raise PreparationError('JSON exceeds 8 MiB')
    try:
        def finite_float(raw):
            value = float(raw)
            if not math.isfinite(value):
                raise PreparationError('Nonfinite JSON number')
            return value
        return json.loads(data.decode('utf-8'), object_pairs_hook=_strict_object, parse_float=finite_float,
                          parse_constant=lambda _: (_ for _ in ()).throw(PreparationError('Nonfinite JSON')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PreparationError('Invalid bounded UTF-8 JSON') from exc


def _identifier(value, pattern, label):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PreparationError('Invalid ' + label)
    return value


def normalize_url(value):
    parts = urlsplit(value)
    try:
        address = ipaddress.ip_address(parts.hostname or '')
        port = parts.port
    except ValueError as exc:
        raise PreparationError('Use an explicit numeric loopback host and port') from exc
    if (parts.scheme != 'http' or not address.is_loopback or not port
            or parts.username is not None or parts.password is not None
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise PreparationError('Only an explicit http loopback origin without credentials/path/query is allowed')
    host = '[' + str(address) + ']' if address.version == 6 else str(address)
    return f'http://{host}:{port}'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PreparationError('HTTP redirects are forbidden')


class Client:
    """Whitelisted local requests; GET retry is bounded, POST is never retried silently."""
    def __init__(self, base_url, timeout=10, get_retries=1):
        self.base_url = normalize_url(base_url)
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise PreparationError('Timeout must be 1..30 seconds')
        if type(get_retries) is not int or not 0 <= get_retries <= 2:
            raise PreparationError('GET retries must be 0..2')
        self.timeout, self.get_retries = timeout, get_retries
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.calls = 0
        self.deadline = time.monotonic() + 180

    @staticmethod
    def _allowed(method, path):
        if method == 'POST':
            return path in ALLOWED_POSTS
        if method != 'GET':
            return False
        route = urlsplit(path)
        if route.scheme or route.netloc or route.fragment or not path.startswith('/api/'):
            return False
        return bool(re.fullmatch(
            r'/api/(?:health|semantic-materials/alpha101_formula|semantic-material-sources/alpha101_paper|'
            r'research-cases/research_case_[0-9a-f]{64}|research-claims/research_claims_[0-9a-f]{64}|'
            r'claim-review-targets/research_claims_[0-9a-f]{64}|claim-reviews/status|'
            r'researches/[A-Za-z0-9_-]{1,200}/workflow-observation-context)', route.path))

    def request(self, method, path, body=None, *, binary=False):
        if not self._allowed(method, path) or (body is not None and method != 'POST'):
            raise PreparationError('Request is outside the preparation route whitelist')
        raw = canonical(body) if body is not None else None
        if raw is not None and len(raw) > 128 * 1024:
            raise PreparationError('Claim request exceeds 128 KiB')
        attempts = 1 + (self.get_retries if method == 'GET' else 0)
        limit = MAX_PDF if binary else MAX_JSON
        for attempt in range(attempts):
            self.calls += 1
            remaining = self.deadline - time.monotonic()
            if self.calls > 96 or remaining <= 0:
                raise PreparationError('Preparation HTTP call budget exhausted')
            req = Request(self.base_url + path, data=raw, method=method,
                          headers={'Accept': 'application/pdf' if binary else 'application/json',
                                   'Content-Type': 'application/json'})
            try:
                with self.opener.open(req, timeout=min(self.timeout, remaining)) as response:
                    if response.geturl() != self.base_url + path:
                        raise PreparationError('Response origin/path changed')
                    length = response.headers.get('Content-Length')
                    if length is not None and (not length.isdigit() or int(length) > limit):
                        raise PreparationError('Response Content-Length exceeds bound or is invalid')
                    chunks, size = [], 0
                    while True:
                        if time.monotonic() > self.deadline:
                            raise PreparationError('Preparation HTTP wall-time budget exhausted')
                        chunk = response.read1(min(64 * 1024, limit + 1 - size))
                        if not chunk:
                            break
                        size += len(chunk)
                        chunks.append(chunk)
                        if size > limit:
                            raise PreparationError('Response exceeds bound')
                    value = b''.join(chunks)
                    if len(value) > limit:
                        raise PreparationError('Response exceeds bound')
                    if length is not None and int(length) != len(value):
                        raise PreparationError('Incomplete HTTP response; a submitted claim may already exist. Resume the saved request.')
                    if binary:
                        if not value.startswith(b'%PDF-'):
                            raise PreparationError('Source is not a PDF')
                        return value
                    value = decode(value)
                    if not isinstance(value, dict):
                        raise PreparationError('Expected JSON object response')
                    return value
            except HTTPError as exc:
                raise PreparationError(f'HTTP {exc.code}; no altered request will be submitted') from exc
            except (URLError, TimeoutError, OSError, ConnectionError, HTTPException) as exc:
                if attempt + 1 == attempts:
                    raise PreparationError('HTTP transport failed; a submitted claim may already exist. Resume the saved request.') from exc
                time.sleep(0.05 * (attempt + 1))


def _safe_dir(value, *, create=False):
    path = Path(value).absolute()
    for part in (path, *path.parents):
        if part.exists() and (part.is_symlink() or not part.is_dir()):
            raise PreparationError('Output paths must be directories without symbolic links')
        if part.is_symlink():
            raise PreparationError('Symbolic links are forbidden')
    if create:
        path.mkdir(parents=True, exist_ok=False)
    if not path.is_dir() or path.resolve() != path:
        raise PreparationError('Output directory is missing or noncanonical')
    return path


def _read(root, name, limit=MAX_JSON):
    if '/' in name or name in ('', '.', '..'):
        raise PreparationError('Unsafe packet filename')
    path = root / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise PreparationError('Packet file must be bounded, regular and not linked: ' + name)
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise PreparationError('Packet file exceeds bound')
    return value


def _sync_dir(root):
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_temp(descriptor, data, name):
    position = 0
    while position < len(data):
        written = os.write(descriptor, data[position:])
        if written <= 0:
            raise PreparationError('Cannot persist preparation output: ' + name)
        position += written


def _cleanup_temps(root):
    allowed = set(DATA_FILES) | {'.prepare.lock'}
    temporary = {'.' + name + '.tmp': name for name in DATA_FILES}
    for path in root.iterdir():
        if path.name in allowed:
            continue
        if path.name not in temporary:
            raise PreparationError('Unknown output entry; no automatic cleanup: ' + path.name)
        info = path.lstat()
        limit = MAX_PDF if temporary[path.name] == 'paper.pdf' else MAX_JSON
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink not in (1, 2):
            raise PreparationError('Unsafe preparation temporary file')
        if info.st_nlink == 2:
            final = root / temporary[path.name]
            final_info = final.lstat()
            if not stat.S_ISREG(final_info.st_mode) or (info.st_dev, info.st_ino) != (final_info.st_dev, final_info.st_ino):
                raise PreparationError('Temporary publication links do not identify one exact output')
        path.unlink()
        _sync_dir(root)


def _save_once(root, name, value):
    if name not in DATA_FILES:
        raise PreparationError('Unknown output filename')
    data = value if isinstance(value, bytes) else canonical(value)
    if len(data) > (MAX_PDF if name == 'paper.pdf' else MAX_JSON):
        raise PreparationError('Output file exceeds bound')
    path = root / name
    if path.exists() or path.is_symlink():
        if _read(root, name, MAX_PDF if name == 'paper.pdf' else MAX_JSON) != data:
            raise PreparationError('Existing output differs; never overwrite: ' + name)
        return
    temporary = root / ('.' + name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _write_temp(descriptor, data, name)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    # link() publishes without replacing an existing final name. A process
    # death between link and unlink leaves only two controlled links to the
    # same complete, fsynced inode; resume removes that known temporary link.
    os.link(temporary, path, follow_symlinks=False)
    _sync_dir(root)
    temporary.unlink()
    _sync_dir(root)


@contextmanager
def _lock(root):
    path = root / '.prepare.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 128:
            raise PreparationError('Preparation lock must be a bounded, regular, unlinked file')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreparationError('Another preparation is active; OS releases the lock after process exit') from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode('ascii'))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)


def _hashes(root, names):
    values, size = {}, 0
    for name in names:
        raw = _read(root, name, MAX_PDF if name == 'paper.pdf' else MAX_JSON)
        size += len(raw)
        if size > MAX_PACKAGE:
            raise PreparationError('Packet exceeds combined 32 MiB')
        values[name] = {'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}
    return values


def _scripts():
    directory = Path(__file__).resolve().parent
    return {name: sha256((directory / name).read_bytes()).hexdigest()
            for name in ('prepare_human_review.py', 'human_review_packet.py')}


def _health(value, workspace, commit=None):
    if (value.get('status') != 'ok' or type(value.get('database_schema')) is not int
            or (value.get('database_schema'), value.get('version')) not in SUPPORTED_RUNTIMES
            or value.get('workspace_id') != workspace
            or value.get('ai_enabled') is not False):
        raise PreparationError('Workbench health/version/schema/workspace/AI identity mismatch')


def _selection(case):
    _identifier(case.get('id'), r'research_case_[0-9a-f]{64}', 'Case identity')
    if case.get('source_kind') != 'daily_run':
        raise PreparationError('Only a frozen daily Alpha101 Case is supported')
    provenance = case.get('context', {}).get('provenance', [])
    values = {item['label']: item['value'] for item in provenance}
    if len(values) != len(provenance):
        raise PreparationError('Duplicate Case provenance')
    selected = {name: _identifier(values.get(name), r'[A-Za-z0-9_-]{1,200}', name)
                for name in ('research_id', 'revision_id', 'attempt_id')}
    selected['run_id'] = _identifier(case.get('source_id'), r'[A-Za-z0-9_-]{1,200}', 'source run')
    return selected


def _context_path(case):
    selection = _selection(case)
    return ('/api/researches/' + selection['research_id'] + '/workflow-observation-context?'
            + urlencode({key: selection[key] for key in ('revision_id', 'run_id', 'attempt_id')}))


def _runtime(value, health, case, expected_commit=None):
    runtime, selected = value.get('runtime', {}), value.get('selected') or {}
    identity = _selection(case)
    commit = _identifier(runtime.get('code_commit'), r'[0-9a-f]{40}', 'verified server commit')
    if (runtime.get('code_commit_verified') is not True or runtime.get('provenance') != 'server_runtime'
            or runtime.get('version') != health['version'] or value.get('compatibility', {}).get('compatible') is not True
            or value.get('protocol', {}).get('candidate_id') != 'alpha101'
            or value.get('protocol', {}).get('engine_mode') != 'normalized_fixed'
            or value.get('research_id') != identity['research_id']
            or value.get('revision_id') != identity['revision_id']
            or selected.get('verified') is not True or selected.get('status') != 'completed'
            or any(selected.get(key) != identity[key] for key in ('revision_id', 'run_id', 'attempt_id'))
            or (expected_commit is not None and commit != expected_commit)):
        raise PreparationError('Incompatible protocol, unverified runtime, or wrong selected source')
    return commit


def _pdf(case, material, raw):
    actual = sha256(raw).hexdigest()
    sources = [item for item in material.get('sources', []) if item.get('source_id') == 'alpha101_paper']
    if len(sources) != 1 or sources[0].get('sha256') != actual or sources[0].get('raw_pdf_reverified') is not True:
        raise PreparationError('PDF does not match the fixed, reverified material source')
    quotes = [item for item in case['context']['evidence']
              if item.get('origin') == 'paper' and actual in item.get('locator', '')]
    if not quotes or not raw.startswith(b'%PDF-'):
        raise PreparationError('PDF does not match the frozen Case citation')


def _initial(root):
    preparation = decode(_read(root, 'preparation.json'))
    if not isinstance(preparation, dict):
        raise PreparationError('Preparation must be an object')
    expected = {'schema_version', 'kind', 'config', 'client_scripts', 'files', 'request_digest',
                'server_runtime_commit', 'preparation_digest', 'scope'}
    if set(preparation) != expected or preparation.get('schema_version') != 1 or preparation.get('kind') != 'alpha101_preparation':
        raise PreparationError('Unknown preparation format')
    if preparation['preparation_digest'] != digest({key: value for key, value in preparation.items() if key != 'preparation_digest'}):
        raise PreparationError('Preparation digest mismatch')
    if preparation['files'] != _hashes(root, INITIAL_FILES):
        raise PreparationError('Prepared input files changed')
    config = preparation['config']
    if set(config) != {'base_url', 'workspace_id', 'case_id', 'timeout', 'get_retries'}:
        raise PreparationError('Unknown saved configuration')
    if normalize_url(config['base_url']) != config['base_url']:
        raise PreparationError('Saved origin is noncanonical')
    _identifier(config['workspace_id'], r'[0-9a-f]{64}', 'workspace identity')
    inputs, request = decode(_read(root, 'inputs.json')), decode(_read(root, 'request.json'))
    if not isinstance(inputs, dict) or set(inputs) != {'case', 'material', 'runtime', 'health'}:
        raise PreparationError('Unknown input snapshots')
    _health(inputs['health'], config['workspace_id'])
    _runtime(inputs['runtime'], inputs['health'], inputs['case'], preparation['server_runtime_commit'])
    validate_inputs(inputs['case'], inputs['material'], inputs['runtime'], inputs['health'])
    if inputs['case']['id'] != config['case_id'] or inputs['material'].get('case_id') != 'alpha101_formula':
        raise PreparationError('Saved Case/material selection mismatch')
    _pdf(inputs['case'], inputs['material'], _read(root, 'paper.pdf', MAX_PDF))
    draft = build_claim_request(inputs['case'], inputs['material'])
    expected_key = 'alpha101-review-prepare-' + digest({'workspace_id': config['workspace_id'],
        'server_commit': preparation['server_runtime_commit'], 'request': draft,
        'paper_sha256': preparation['files']['paper.pdf']['sha256']})
    if request != {**draft, 'idempotency_key': expected_key} or preparation['request_digest'] != digest(request):
        raise PreparationError('Saved request/key changed')
    return preparation, inputs, request


def _live_inputs(client, preparation, inputs):
    config = preparation['config']
    health = client.request('GET', '/api/health')
    _health(health, config['workspace_id'])
    case = client.request('GET', '/api/research-cases/' + config['case_id'])
    material = client.request('GET', '/api/semantic-materials/alpha101_formula')
    runtime = client.request('GET', _context_path(case))
    _runtime(runtime, health, case, preparation['server_runtime_commit'])
    validate_inputs(case, material, runtime, health)
    if (case != inputs['case'] or material != inputs['material']
            or runtime.get('protocol') != inputs['runtime'].get('protocol')
            or runtime['runtime'].get('code_digest') != inputs['runtime']['runtime'].get('code_digest')):
        raise PreparationError('Prepared sources, protocol or accepted runtime changed')
    pdf = client.request('GET', '/api/semantic-material-sources/alpha101_paper', binary=True)
    _pdf(case, material, pdf)
    if sha256(pdf).hexdigest() != preparation['files']['paper.pdf']['sha256']:
        raise PreparationError('Live PDF differs from the frozen packet')


def _finish(root, client, preparation, inputs, request):
    current_preparation, current_inputs, current_request = _initial(root)
    if (current_preparation != preparation or current_inputs != inputs or current_request != request):
        raise PreparationError('Prepared request changed before submission')
    _live_inputs(client, preparation, inputs)
    draft = {key: value for key, value in request.items() if key != 'idempotency_key'}
    preview = client.request('POST', '/api/research-claims/preview', draft)
    _save_once(root, 'preview.json', preview)
    if (root / 'claims.json').exists():
        saved = decode(_read(root, 'claims.json'))
        claims_id = _identifier(saved.get('id'), r'research_claims_[0-9a-f]{64}', 'claims identity')
        claims = client.request('GET', '/api/research-claims/' + claims_id)
    else:
        claims = client.request('POST', '/api/research-claims', request)
    _save_once(root, 'claims.json', claims)
    if (claims.get('submitted_claims') != request['claims']
            or claims.get('case_id') != request['case_id'] or claims.get('case_digest') != request['case_digest']
            or preview != {key: claims[key] for key in ('case_id', 'case_digest', 'claims', 'limitations')}):
        raise PreparationError('Returned claims do not match the persisted exact request/preview')
    claims_id = _identifier(claims.get('id'), r'research_claims_[0-9a-f]{64}', 'claims identity')
    targets, statuses = [], []
    for draft_claim in request['claims']:
        query = urlencode({'claim_id': draft_claim['id']})
        target = client.request('GET', '/api/claim-review-targets/' + claims_id + '?' + query)
        status = client.request('GET', '/api/claim-reviews/status?' + urlencode({'claims_id': claims_id, 'claim_id': draft_claim['id']}))
        if status.get('target') != target:
            raise PreparationError('Review status is bound to another target')
        targets.append(target)
        statuses.append(status)
    _save_once(root, 'targets.json', targets)
    _save_once(root, 'statuses.json', statuses)
    packet = build_packet(inputs['case'], inputs['material'], claims, targets, inputs['runtime'], inputs['health'])
    if verify_packet(packet).get('passed') is not True:
        raise PreparationError('Packet failed independent snapshot verification')
    _save_once(root, 'packet.json', packet)
    lines = ['# Alpha101 待审材料', '', '这是 automation/draft；没有提交人工判断，也没有调用 AI。',
             '', '先打开 paper.pdf 第 15 页核对公式，再在工作台选择原 Case 和下列准确结论。',
             '逐条阅读证据、服务端指标及限制，再由本人填写五维审核；历史简单审核不代替本次判断。',
             '', 'Case: ' + request['case_id'], 'Claims: ' + claims_id,
             '服务端验收 commit: ' + preparation['server_runtime_commit'],
             '客户端代码摘要另保存在 preparation.json；本工具没有升级工作台。', '']
    for target, status in zip(targets, statuses):
        lines.extend(['- ' + target['claim_id'] + ': ' + target['target_digest']
                      + '; 导出时人工声明状态 ' + status['human_declared_status']])
    lines += ['', '离线验证只核对本地快照和内部绑定，不认证作者、真人身份或语义真伪。',
              '材料参考稿不是标准答案；模型质量、真实人工效果和人工耗时尚未评测。', '']
    _save_once(root, 'report.md', '\n'.join(lines).encode('utf-8'))
    manifest = {'schema_version': 1, 'kind': 'alpha101_human_review_export',
                'preparation_digest': preparation['preparation_digest'], 'packet_digest': packet['packet_digest'],
                'claims_id': claims_id, 'server_runtime_commit': preparation['server_runtime_commit'],
                'client_scripts': preparation['client_scripts'], 'files': _hashes(root, FINAL_FILES),
                'scope': 'Frozen local snapshot integrity only; no human declarations or model quality generated.'}
    manifest['manifest_digest'] = digest(manifest)
    _save_once(root, 'manifest.json', manifest)
    return verify(root)


def prepare(base_url, workspace_id, case_id, out, *, server_commit=None, timeout=10, get_retries=1):
    _identifier(workspace_id, r'[0-9a-f]{64}', 'workspace identity')
    _identifier(case_id, r'research_case_[0-9a-f]{64}', 'Case identity')
    if server_commit is not None:
        _identifier(server_commit, r'[0-9a-f]{40}', 'expected server commit')
    client = Client(base_url, timeout, get_retries)
    health = client.request('GET', '/api/health')
    _health(health, workspace_id)
    case = client.request('GET', '/api/research-cases/' + case_id)
    if case.get('id') != case_id:
        raise PreparationError('Server returned a different Case')
    material = client.request('GET', '/api/semantic-materials/alpha101_formula')
    runtime = client.request('GET', _context_path(case))
    commit = _runtime(runtime, health, case, server_commit)
    validate_inputs(case, material, runtime, health)
    pdf = client.request('GET', '/api/semantic-material-sources/alpha101_paper', binary=True)
    _pdf(case, material, pdf)
    draft = build_claim_request(case, material)
    key = 'alpha101-review-prepare-' + digest({'workspace_id': workspace_id, 'server_commit': commit,
        'request': draft, 'paper_sha256': sha256(pdf).hexdigest()})
    request = {**draft, 'idempotency_key': key}
    root = _safe_dir(out, create=True)
    with _lock(root):
        _save_once(root, 'inputs.json', {'case': case, 'material': material, 'runtime': runtime, 'health': health})
        _save_once(root, 'request.json', request)
        _save_once(root, 'paper.pdf', pdf)
        preparation = {'schema_version': 1, 'kind': 'alpha101_preparation',
            'config': {'base_url': client.base_url, 'workspace_id': workspace_id, 'case_id': case_id,
                       'timeout': timeout, 'get_retries': get_retries},
            'client_scripts': _scripts(), 'files': _hashes(root, INITIAL_FILES),
            'request_digest': digest(request), 'server_runtime_commit': commit,
            'scope': 'Exact request saved before POST; no label/review/experiment/observation writes.'}
        preparation['preparation_digest'] = digest(preparation)
        _save_once(root, 'preparation.json', preparation)
        return _finish(root, client, preparation, decode(_read(root, 'inputs.json')), request)


def resume(out):
    root = _safe_dir(out)
    with _lock(root):
        _cleanup_temps(root)
        if (root / 'manifest.json').exists():
            return verify(root)
        preparation, inputs, request = _initial(root)
        if preparation['client_scripts'] != _scripts():
            raise PreparationError('Prepared client implementation changed; do not silently resume with new logic')
        config = preparation['config']
        return _finish(root, Client(config['base_url'], config['timeout'], config['get_retries']), preparation, inputs, request)


def verify(out, *, live=False):
    root = _safe_dir(out)
    # This coordination file is intentionally not a research artifact hash.
    # flock ownership is maintained by the OS, not by the advisory PID text.
    _read(root, '.prepare.lock', 128)
    preparation, inputs, request = _initial(root)
    manifest = decode(_read(root, 'manifest.json'))
    expected = {'schema_version', 'kind', 'preparation_digest', 'packet_digest', 'claims_id',
                'server_runtime_commit', 'client_scripts', 'files', 'scope', 'manifest_digest'}
    if set(manifest) != expected or manifest['kind'] != 'alpha101_human_review_export' or manifest['schema_version'] != 1:
        raise PreparationError('Unknown manifest format')
    if manifest['manifest_digest'] != digest({key: value for key, value in manifest.items() if key != 'manifest_digest'}):
        raise PreparationError('Manifest digest mismatch')
    if manifest['files'] != _hashes(root, FINAL_FILES):
        raise PreparationError('Exported packet file changed')
    if set(item.name for item in root.iterdir()) - {'.prepare.lock'} != set(FINAL_FILES) | {'manifest.json'}:
        raise PreparationError('Unexpected packet entries')
    packet, claims = decode(_read(root, 'packet.json')), decode(_read(root, 'claims.json'))
    preview = decode(_read(root, 'preview.json'))
    targets, statuses = decode(_read(root, 'targets.json')), decode(_read(root, 'statuses.json'))
    if (manifest['preparation_digest'] != preparation['preparation_digest']
            or manifest['client_scripts'] != preparation['client_scripts']
            or manifest['server_runtime_commit'] != preparation['server_runtime_commit']
            or manifest['packet_digest'] != packet.get('packet_digest') or manifest['claims_id'] != claims.get('id')
            or claims.get('submitted_claims') != request['claims']
            or preview != {key: claims[key] for key in ('case_id', 'case_digest', 'claims', 'limitations')}
            or packet.get('case') != inputs['case'] or packet.get('material') != inputs['material']
            or packet.get('runtime') != inputs['runtime'] or packet.get('health') != inputs['health']
            or packet.get('claims') != claims or packet.get('targets') != targets
            or len(statuses) != len(targets)
            or any(status.get('target') != target for status, target in zip(statuses, targets))):
        raise PreparationError('Exported snapshots and identities disagree')
    verdict = verify_packet(packet)
    if verdict.get('passed') is not True:
        raise PreparationError('Packet snapshot verification failed')
    current_status = None
    if live:
        config = preparation['config']
        client = Client(config['base_url'], config['timeout'], config['get_retries'])
        _live_inputs(client, preparation, inputs)
        if client.request('GET', '/api/research-claims/' + claims['id']) != claims:
            raise PreparationError('Live claims differ from frozen drafts')
        current_status = []
        for target in targets:
            query = urlencode({'claims_id': claims['id'], 'claim_id': target['claim_id']})
            status = client.request('GET', '/api/claim-reviews/status?' + query)
            if status.get('target') != target:
                raise PreparationError('Live exact target differs from frozen target')
            current_status.append({'claim_id': target['claim_id'], 'human_declared_status': status['human_declared_status'],
                                   'human_records': status['human_records'], 'semantic_quality_score': status['semantic_quality_score']})
    return {'passed': True, 'mode': 'live_read_only' if live else 'offline', 'out': str(root),
            'case_id': request['case_id'], 'claims_id': claims['id'], 'claim_ids': [item['id'] for item in request['claims']],
            'manifest_digest': manifest['manifest_digest'], 'server_runtime_commit': preparation['server_runtime_commit'],
            'client_scripts': preparation['client_scripts'], 'packet_verification': verdict,
            'current_status': current_status, 'human_judgments_generated': 0, 'llm_api_called': False,
            'scope': 'Snapshot integrity and exact bindings; not authorship, semantic truth, or model quality.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest='mode', required=True)
    create = modes.add_parser('prepare')
    create.add_argument('--base-url', required=True)
    create.add_argument('--workspace-id', required=True)
    create.add_argument('--case-id', required=True)
    create.add_argument('--out', required=True, type=Path)
    create.add_argument('--server-commit')
    create.add_argument('--timeout', type=float, default=10)
    create.add_argument('--get-retries', type=int, default=1)
    continuation = modes.add_parser('resume')
    continuation.add_argument('--out', required=True, type=Path)
    check = modes.add_parser('verify')
    check.add_argument('--out', required=True, type=Path)
    check.add_argument('--live', action='store_true')
    args = vars(parser.parse_args(argv)); mode = args.pop('mode')
    try:
        result = {'prepare': prepare, 'resume': resume, 'verify': verify}[mode](**args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (PreparationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'passed': False, 'mode': mode, 'error': str(exc),
                          'scope': 'No automatic human judgment; saved requests are retained for explicit resume.'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
