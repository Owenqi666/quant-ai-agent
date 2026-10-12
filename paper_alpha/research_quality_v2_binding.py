"""Read-only development-result bindings. No Store initialization, service mutation or model dispatch.

The caller must supply the original host manifest SHA from outside observations.
This authenticates byte/projection readback, not scientific truth or actual model
invocation. Final-period inputs and private data paths are never dereferenced.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

MAX_JSON_BYTES = 2 * 1024 * 1024


class QualityBindingError(ValueError):
    pass


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise QualityBindingError('Noncanonical or nonfinite JSON') from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf8')).hexdigest()


def _sha(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise QualityBindingError('Invalid SHA256')


def _fields(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise QualityBindingError('Unknown or missing binding fields')


def _name(value):
    if type(value) is not str or not re.fullmatch('[A-Za-z0-9_:.-]{1,128}', value):
        raise QualityBindingError('Invalid binding identity')


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise QualityBindingError('Duplicate JSON field')
        out[key] = value
    return out


def read_bounded_json(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise QualityBindingError('Symlinked binding path')
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError as exc:
        raise QualityBindingError('Cannot read binding file') from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_JSON_BYTES:
            raise QualityBindingError('Nonregular, hardlinked or oversized binding file')
        raw = b''
        while len(raw) <= MAX_JSON_BYTES:
            chunk = os.read(fd, min(65536, MAX_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(fd)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(raw) > MAX_JSON_BYTES or signature(before) != signature(after) or signature(after) != signature(path.stat()):
            raise QualityBindingError('Binding file changed during read')
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode('utf8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(QualityBindingError('Nonfinite JSON')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise QualityBindingError('Malformed bounded JSON') from exc
    canonical(value)
    return value, hashlib.sha256(raw).hexdigest()


def _metrics(value):
    if type(value) is not dict or len(value) > 128:
        raise QualityBindingError('Invalid metric inventory')
    for key, item in value.items():
        _name(key)
        try:
            valid = item is None or type(item) in (int, float) and math.isfinite(item)
        except OverflowError:
            valid = False
        if not valid:
            raise QualityBindingError('Metrics must be finite numbers or explicit null')


@dataclass(frozen=True)
class HostRunBinding:
    manifest_path: str
    expected_manifest_sha256: str
    snapshot_json: str

    def to_dict(self):
        current = _bind(self.manifest_path, self.expected_manifest_sha256)
        if canonical(current) != self.snapshot_json:
            raise QualityBindingError('Frozen binding differs from current host bytes')
        return json.loads(self.snapshot_json)


def _bind(manifest_path, expected_manifest_sha256):
    _sha(expected_manifest_sha256)
    path = Path(manifest_path).absolute()
    manifest, actual = read_bounded_json(path)
    if actual != expected_manifest_sha256:
        raise QualityBindingError('Original external manifest SHA differs')
    _fields(manifest, ('schema_version', 'run_id', 'task_id', 'mode', 'attempt_id', 'partition',
                      'final_numeric_reads', 'identities', 'code_revision', 'status', 'files'))
    if type(manifest['schema_version']) is not int or manifest['schema_version'] != 1:
        raise QualityBindingError('Unknown host manifest version')
    for k in ('run_id', 'task_id', 'attempt_id', 'code_revision'):
        _name(manifest[k])
    if manifest['mode'] not in ('fixed', 'deterministic_repair', 'model_assisted') or manifest['status'] not in ('completed', 'abstained', 'failed', 'cancelled', 'budget_stopped', 'not_run'):
        raise QualityBindingError('Invalid mode or terminal status')
    if manifest['partition'] != 'development' or type(manifest['final_numeric_reads']) is not int or manifest['final_numeric_reads'] != 0:
        raise QualityBindingError('Only explicit development bindings with zero final reads are admitted')
    _fields(manifest['identities'], ('source', 'task', 'config', 'catalog', 'exposure'))
    for val in manifest['identities'].values():
        _sha(val)
    files = manifest['files']
    if type(files) is not dict or set(files) not in ({'evaluation', 'report'}, {'evaluation', 'report', 'factor_values'}):
        raise QualityBindingError('Frozen result role inventory differs')
    results, shas = {}, {}
    for role, item in files.items():
        _fields(item, ('path', 'sha256'))
        _sha(item['sha256'])
        if type(item['path']) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}\.json', item['path']) or item['path'] in ('.json', '..json') or item['path'] == path.name:
            raise QualityBindingError('Result path must be a distinct local basename')
        result, sha = read_bounded_json(path.parent / item['path'])
        if sha != item['sha256']:
            raise QualityBindingError('Frozen result SHA differs')
        _fields(result, ('run_id', 'task_id', 'attempt_id', 'values' if role == 'factor_values' else 'metrics'))
        for key in ('run_id', 'task_id', 'attempt_id'):
            if result[key] != manifest[key]:
                raise QualityBindingError('Cross-run result identity differs')
        if role != 'factor_values':
            _metrics(result['metrics'])
        else:
            _fields(result['values'], ('reference_id', 'input_digest', 'values'))
            _name(result['values']['reference_id']); _sha(result['values']['input_digest'])
            _metrics(result['values']['values'])
        results[role], shas[role] = result, sha
    if read_bounded_json(path)[1] != actual:
        raise QualityBindingError('Manifest changed during binding')
    return {'manifest_sha256': actual, 'manifest': manifest, 'results': results,
            'file_sha256': shas, 'binding_scope': 'host_byte_and_metric_projection_readback',
            'model_invocation_authenticated': False, 'scientific_score': None}


def bind_host_run(manifest_path, expected_manifest_sha256):
    value = _bind(manifest_path, expected_manifest_sha256)
    return HostRunBinding(str(Path(manifest_path).absolute()), expected_manifest_sha256, canonical(value))


def verify_report_metrics(binding, claims):
    if type(binding) is not HostRunBinding:
        raise QualityBindingError('A verified external host binding is required')
    _metrics(claims)
    data = binding.to_dict()
    actual = data['results']['evaluation']['metrics']
    report = data['results']['report']['metrics']
    if set(claims) - set(actual) or set(report) - set(actual):
        raise QualityBindingError('Foreign metric claim')
    rows = []
    for key in sorted(actual):
        expected = actual[key]
        state = 'unknown' if expected is None or key not in claims or claims[key] is None or key not in report or report[key] is None else 'passed' if expected == claims[key] == report[key] else 'failed'
        rows.append({'key': key, 'state': state})
    return {'scope': data['binding_scope'], 'denominator': len(actual),
            'passed': sum(r['state'] == 'passed' for r in rows), 'rows': rows,
            'scientific_score': None}


# Independent readers of original production evidence. The two canonical digest
# encodings are deliberately separate: storage JSON includes indentation/newline,
# provider wire JSON does not. Neither reader writes or re-signs any evidence.
def _storage_digest(value):
    return hashlib.sha256((json.dumps(value, ensure_ascii=False, sort_keys=True,
        indent=2, allow_nan=False) + "\n").encode('utf8')).hexdigest()


def _decode_record(value):
    if type(value) is not str or len(value.encode('utf8')) > MAX_JSON_BYTES:
        raise QualityBindingError('Original ledger record exceeds bound')
    try:
        result = json.loads(value, object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(QualityBindingError('Nonfinite ledger JSON')))
        canonical(result)
        return result
    except (ValueError, TypeError, RecursionError) as exc:
        raise QualityBindingError('Malformed original ledger record') from exc


def _need(condition, message):
    if not condition:
        raise QualityBindingError(message)


def _original_identity(value):
    _need(type(value) is str and bool(re.fullmatch('[A-Za-z0-9_-]{1,128}', value)),
          'Invalid original business identity')


def _original_connection(root):
    import sqlite3
    root = Path(root).absolute()
    path = root / 'workbench.sqlite3'
    _need('..' not in root.parts and not any(p.is_symlink() for p in (path, *path.parents)),
          'Unsafe original Store path')
    _need(path.is_file() and path.stat().st_nlink == 1, 'Original Store database is missing or unsafe')
    _need(not any(Path(str(path) + suffix).is_symlink() for suffix in ('-wal', '-shm', '-journal')),
          'Unsafe original SQLite sidecar')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    return root, connection


def _one(connection, table, identity):
    # Table names only come from adapter source; caller values are parameterized.
    row = connection.execute('SELECT * FROM ' + table + ' WHERE id=?', (identity,)).fetchone()
    _need(row is not None, 'Explicit original ' + table + ' identity is missing')
    return dict(row)


def _unresolved(kind, missing):
    return {'schema_version': 1, 'status': 'unresolved', 'adapter_kind': kind,
        'reason_code': 'EXTERNAL_PIN_REQUIRED', 'missing_pins': missing,
        'binding_scope': 'none', 'model_invocation_authenticated': False,
        'native_qualified': False, 'model_quality_qualified': False,
        'scientific_score': None, 'final_numeric_reads': 0}


def _original_bytes(path, limit=MAX_JSON_BYTES):
    path = Path(path).absolute()
    _need(not any(p.is_symlink() for p in (path, *path.parents)), 'Symlinked original proof')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= limit,
              'Original proof must be a bounded private regular file')
        raw = b''
        while len(raw) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not part:
                break
            raw += part
        after = os.fstat(fd)
        sig = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
        _need(len(raw) <= limit and sig(before) == sig(after) == sig(path.stat()), 'Original proof changed')
        return raw, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def bind_original_daily_run(store_root, *, run_id, attempt_id, expected_manifest_sha256=None):
    """Read a pinned original D manifest v2 and published business anchors.

    Reads explicit JSON roles only, never a dataset CSV, directory inventory,
    current code, Store object or numeric evaluator. This proves result/report
    projection consistency; source realism and scientific fidelity remain unknown.
    """
    if expected_manifest_sha256 is None:
        return _unresolved('original_daily_v2', ['expected_manifest_sha256'])
    _sha(expected_manifest_sha256); _original_identity(run_id); _original_identity(attempt_id)
    root, connection = _original_connection(store_root)
    try:
        run = _one(connection, 'runs', run_id)
        attempt = _one(connection, 'attempts', attempt_id)
        _need(run['attempt_id'] == attempt_id and attempt['run_id'] == run_id and
              run['worker_id'] == attempt['worker_id'], 'Original run/attempt ownership differs')
        _need(run['status'] == attempt['status'] == 'completed', 'Original D attempt is not published completed')
        out = root / 'runs' / run_id / 'attempts' / attempt_id / 'output'
        _need(Path(attempt['output_dir']).absolute() == out, 'Original output location differs')
        manifest_path = out / 'manifest.json'
        manifest, sha = read_bounded_json(manifest_path)
        _need(sha == expected_manifest_sha256, 'External original D manifest pin differs')
        _need(type(manifest.get('schema_version')) is int and manifest['schema_version'] == 2 and
              manifest.get('run_version') == 'registered-daily-workflow-v2', 'Unsupported original D manifest')
        _need(manifest.get('run_id') == run_id and manifest.get('attempt_id') == attempt_id and
              manifest.get('worker_id') == attempt['worker_id'] and manifest.get('mode') == run['mode'],
              'D manifest original business identities differ')
        _need(type(manifest.get('final_numeric_rows_parsed')) is int and manifest['final_numeric_rows_parsed'] == 0 and
              manifest.get('evaluation_scope') == 'development_prefix_validation_only', 'D output is not development only')
        files = manifest.get('files')
        _need(type(files) is dict and len(files) <= 256, 'Invalid bounded D file map')
        checked_files = {}
        def role(name):
            _need(name in files and not Path(name).is_absolute() and '..' not in Path(name).parts,
                  'Explicit D JSON role is missing or unsafe')
            _sha(files[name])
            value, actual = read_bounded_json(out / name)
            _need(actual == files[name], 'Pinned D role bytes differ')
            checked_files[name] = actual
            return value
        state, report = role('state.json'), role('report.json')
        task = state['task']
        revision = _one(connection, 'revisions', run['revision_id'])
        _need(revision['research_id'] == run['research_id'] and _decode_record(revision['task']) == task and
              revision['digest'] == _storage_digest(task) == manifest['task_digest'], 'Original D task/revision differs')
        _need(type(task.get('schema_version')) is int and task['schema_version'] == 2 and task['execution']['kind'] == 'registered_daily_development_v2' and
              task['execution'] == manifest['execution'], 'Original closed D execution differs')
        anchor = connection.execute('SELECT * FROM daily_revision_bindings WHERE revision_id=?', (revision['id'],)).fetchone()
        _need(anchor is not None, 'Original D immutable revision anchor missing')
        anchor = dict(anchor); payload = _decode_record(anchor['payload'])
        _need(anchor['binding_digest'] == _storage_digest(payload) and
              all(anchor[k] == v for k, v in payload.items()) and
              payload['task_digest'] == revision['digest'] and payload['research_id'] == run['research_id'] and
              all(payload.get(k) == v for k, v in task['execution'].items() if k not in ('schema_version', 'kind')),
              'Original D immutable revision anchor differs')
        research = _one(connection, 'researches', run['research_id'])
        dataset = _one(connection, 'datasets', research['dataset_id'])
        paper = _one(connection, 'papers', research['paper_id'])
        registration = _one(connection, 'daily_snapshot_registrations', payload['registration_id'])
        _need(registration['status'] == 'registered' and registration['registered_dataset_id'] == dataset['id'] and
              all(registration[k] == payload[k] for k in ('registration_digest', 'declaration_digest', 'input_digest',
                  'development_content_digest', 'development_index_digest')), 'Original D registration differs')
        registration_identity = {'registration_version': 'daily-snapshot-registration-v1',
            'registration_id': registration['id'], 'declaration_digest': registration['declaration_digest'],
            'dataset_id': dataset['id'], 'dataset_sha256': dataset['sha256'],
            'development_content_digest': registration['development_content_digest'],
            'development_index_digest': registration['development_index_digest']}
        _need(_storage_digest(registration_identity) == registration['registration_digest'] and
              _storage_digest(task['evaluation']) == payload['config_digest'] and
              _storage_digest({k: v for k, v in task['execution'].items() if k != 'source_digest'}) == payload['source_digest'],
              'Original D registration/config/source identities differ')
        metadata = role('inputs/metadata.json')
        _need(dataset['id'] == payload['dataset_id'] and dataset['sha256'] == payload['registered_dataset_digest'] and
              _decode_record(dataset['metadata']) == metadata and
              _storage_digest(metadata) == payload['metadata_digest'] == manifest['prefix_metadata_digest'] and
              metadata.get('data_kind') == 'daily_development_snapshot', 'Original D prefix metadata differs')
        _need(paper['sha256'] == manifest['paper_sha256'] and
              _storage_digest(role('inputs/paper.json')) == manifest['paper_document_digest'] and
              role('inputs/task.json') == task, 'D public task/paper snapshots differ')
        _need(_storage_digest(state) == manifest['state_digest'] == attempt['state_digest'] and
              _decode_record(run['state']) == state and state['mode'] == run['mode'] and state['status'] == 'completed',
              'D state differs from its original publication')
        results, metrics, result_digests = {}, {}, {}
        _need(type(state['candidates']) is list and len(state['candidates']) <= 100, 'D candidate bound exceeded')
        all_candidates = []
        for candidate in state['candidates']:
            identity = candidate['id']; _original_identity(identity)
            _need(identity not in all_candidates, 'Duplicate D candidate')
            all_candidates.append(identity)
            if candidate['status'] not in ('evaluated', 'not_evaluable'):
                continue
            result = role('candidates/' + identity + '/attempt-1/result.json')
            actual = _storage_digest(result)
            _need(actual == candidate['result_digest'] == _storage_digest(candidate['result']) and
                  result['status'] == candidate['status'] and result['evaluation_scope'] == manifest['evaluation_scope'] and
                  result['market_metadata'] == metadata and result['config'] == task['evaluation'], 'D actual result differs')
            _metrics(result['metrics'])
            results[identity], metrics[identity], result_digests[identity] = result, result['metrics'], actual
        _need(report['state_digest'] == manifest['state_digest'] and report['candidate_results'] == result_digests and
              canonical(report['metrics']) == canonical(metrics), 'D report metrics differ from original results')
        for name, expected in checked_files.items():
            _need(read_bounded_json(out / name)[1] == expected, 'D role changed during binding')
        _need(read_bounded_json(manifest_path)[1] == sha, 'Original D manifest changed during binding')
        return {'schema_version': 1, 'status': 'bound', 'adapter_kind': 'original_daily_v2',
            'run_id': run_id, 'attempt_id': attempt_id, 'revision_id': revision['id'],
            'manifest_sha256': sha, 'task_digest': revision['digest'], 'execution': task['execution'],
            'state_digest': manifest['state_digest'], 'checked_files': checked_files,
            'candidate_denominator': len(all_candidates), 'candidate_statuses': {c['id']: c['status'] for c in state['candidates']},
            'metrics': metrics, 'result_digests': result_digests,
            'binding_scope': 'original_development_result_report_and_business_anchor_readback',
            'model_invocation_authenticated': False, 'native_qualified': False,
            'model_quality_qualified': False, 'scientific_score': None, 'final_numeric_reads': 0}
    except (KeyError, TypeError) as exc:
        raise QualityBindingError('Malformed original D evidence') from exc
    finally:
        connection.close()


def bind_original_proposal_attempt(store_root, *, proposal_id, attempt_id,
        expected_receipt_sha256=None, expected_context_sha256=None, expected_raw_sha256=None):
    """Bind original N transport evidence without claiming Native qualification.

    Failed/error/abstained output is retained in the caller's denominator. Even
    invocation_started + successful receipt is insufficient Native/model proof.
    Original native authority/profile/model-intent evaluation is a separate gate.
    """
    pins = {'expected_receipt_sha256': expected_receipt_sha256,
            'expected_context_sha256': expected_context_sha256, 'expected_raw_sha256': expected_raw_sha256}
    missing = [k for k, v in pins.items() if v is None]
    if missing:
        return _unresolved('original_proposal_transport', missing)
    for pin in pins.values(): _sha(pin)
    _original_identity(proposal_id); _original_identity(attempt_id)
    root, connection = _original_connection(store_root)
    try:
        row = _one(connection, 'proposal_requests', proposal_id)
        arow = _one(connection, 'proposal_attempts', attempt_id)
        _need(arow['proposal_id'] == proposal_id, 'Original proposal/attempt ownership differs')
        body, state, attempt = _decode_record(row['payload']), _decode_record(row['state']), _decode_record(arow['payload'])
        _need(_storage_digest(body) == row['payload_digest'] and _storage_digest(state) == row['state_digest'] and
              _storage_digest(body['request']) == row['request_digest'] and proposal_id == 'proposal_' + row['request_digest'] and
              row['status'] == state['status'] and state['current_attempt_id'] == attempt_id and
              body['request'].get('research_id') == row['research_id'] and
              body['request'].get('base_revision_id') == row['base_revision_id'] and
              body['request']['idempotency_key'] == row['idempotency_key'] and
              body['created_at'] == row['created_at'] and body['deadline_at'] == row['deadline_at'] and
              attempt['id'] == attempt_id and attempt['sequence'] == arow['sequence'] and
              _storage_digest(attempt) == arow['digest'], 'Original N request/state/attempt anchor differs')
        records = connection.execute('SELECT * FROM proposal_events WHERE proposal_id=? ORDER BY sequence LIMIT 33', (proposal_id,)).fetchall()
        _need(0 < len(records) <= 32 and _storage_digest([r['digest'] for r in records]) == row['ledger_digest'],
              'Original N event ledger differs')
        last_anchor = None
        charged_digest, origin_digest = None, None
        for i, eventrow in enumerate(records, 1):
            event = _decode_record(eventrow['payload'])
            _need(eventrow['sequence'] == event['sequence'] == i and _storage_digest(event) == eventrow['digest'],
                  'Original N event sequence or bytes differ')
            if event.get('attempt_id') == attempt_id:
                last_anchor = event.get('attempt_digest')
                if event['kind'] == 'attempt_charged':
                    charged_digest, origin_digest = event['attempt_digest'], event.get('origin_digest')
        _need(last_anchor == arow['digest'], 'Original N attempt lacks matching event anchor')
        originrow = connection.execute('SELECT * FROM provider_attempt_origins WHERE attempt_id=?', (attempt_id,)).fetchone()
        _need(originrow is not None, 'Original N attempt origin missing')
        origin = _decode_record(originrow['payload'])
        _need(originrow['digest'] == _storage_digest(origin) == origin_digest and origin['proposal_id'] == proposal_id and
              origin['attempt_id'] == attempt_id and origin['fencing_token'] == attempt['fencing_token'] and
              origin['attempt_charged_digest'] == charged_digest and
              attempt['fencing_token'] == _storage_digest({'attempt': attempt_id, 'worker': attempt['worker_id']}),
              'Original N attempt origin/fencing differs')
        folder = root / 'proposals' / proposal_id
        attempt_folder = folder / 'attempts' / attempt_id
        context, context_sha = read_bounded_json(folder / 'context.json')
        request, _ = read_bounded_json(folder / 'request.json')
        config, _ = read_bounded_json(folder / 'provider-config.json')
        prepared, _ = read_bounded_json(attempt_folder / 'prepared.json')
        receipt, receipt_sha = read_bounded_json(attempt_folder / 'receipt.json')
        raw, raw_sha = _original_bytes(attempt_folder / 'response.raw.json', min(MAX_JSON_BYTES, body['request']['budget']['max_output_bytes']))
        _need(context_sha == expected_context_sha256 and receipt_sha == expected_receipt_sha256 and raw_sha == expected_raw_sha256,
              'External original N evidence pin differs')
        _need(context == body['context'] and digest(context) == body['context_digest'] and request == body['request'] and
              config == body['config'], 'Original N frozen context/request/config differ')
        _need(_storage_digest(prepared) == attempt['prepared_digest'] and prepared['proposal_id'] == proposal_id and
              prepared['attempt_id'] == attempt_id and prepared['context_digest'] == body['context_digest'] and
              prepared['provider_config'] == config and prepared['budget'] == request['budget'], 'Original N prepared input differs')
        _need(receipt == attempt['receipt'] and receipt.get('proposal_id') == proposal_id and receipt.get('attempt_id') == attempt_id and
              receipt.get('response_sha256') == raw_sha == attempt['raw_sha256'], 'Original N receipt/raw proof differs')
        _need(attempt['status'] not in ('running', 'queued') and type(receipt.get('fixture')) is bool and
              type(receipt.get('invocation_started')) is bool and type(receipt.get('process_stopped')) is bool,
              'Original N transport is nonterminal or lacks explicit receipt flags')
        parsed_digest = attempt.get('response_digest')
        if parsed_digest is not None:
            parsed, _ = read_bounded_json(attempt_folder / 'parsed.json')
            try:
                raw_value = _decode_record(raw.decode('utf8'))
            except UnicodeError as exc:
                raise QualityBindingError('Parsed response has malformed raw bytes') from exc
            _need(digest(parsed) == parsed_digest and canonical(parsed) == canonical(raw_value) and
                  parsed.get('proposal_id') == proposal_id and parsed.get('attempt_id') == attempt_id and
                  parsed.get('context_digest') == body['context_digest'], 'Original N parsed/raw identities differ')
        _need(read_bounded_json(folder / 'context.json')[1] == context_sha and
              read_bounded_json(attempt_folder / 'receipt.json')[1] == receipt_sha and
              _original_bytes(attempt_folder / 'response.raw.json')[1] == raw_sha, 'Original N proof changed during binding')
        return {'schema_version': 1, 'status': 'bound', 'adapter_kind': 'original_proposal_transport',
            'proposal_id': proposal_id, 'attempt_id': attempt_id, 'attempt_status': attempt['status'],
            'receipt_sha256': receipt_sha, 'context_sha256': context_sha, 'context_digest': body['context_digest'],
            'raw_sha256': raw_sha, 'original_ledger_digest': row['ledger_digest'],
            'fixture': receipt['fixture'], 'transport_invocation_reported': receipt['invocation_started'],
            'process_stopped': receipt['process_stopped'], 'denominator_disposition': 'retain_this_attempt_including_errors_or_abstention',
            'binding_scope': 'original_ledger_frozen_input_receipt_and_raw_transport_readback',
            'model_invocation_authenticated': False, 'native_qualified': False, 'model_quality_qualified': False,
            'qualification_reason': 'Native authority profile model intent and scientific fidelity are independent unverified gates',
            'scientific_score': None, 'final_numeric_reads': 0}
    except (KeyError, TypeError) as exc:
        raise QualityBindingError('Malformed original N evidence') from exc
    finally:
        connection.close()
