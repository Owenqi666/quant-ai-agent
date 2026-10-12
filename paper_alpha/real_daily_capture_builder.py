"""Metadata-only plans for retained DAILY originals and the existing main lane.

No function opens, stats or hashes a market original, creates a business DB,
grants source permission, or materializes a capture's raw directory. The packet
uses the existing closed capture request; physical import remains a separate
host-reviewed operation. Supported facts are explicit evidence declarations.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from datetime import datetime, timedelta
from .real_daily_admission import FACTS, MAX_METADATA
from .real_daily_main_metadata import parse_main_assessment_request, path_literal, pin, snapshot
from .real_daily_method import assess_events, method_digest
from .real_daily_web_contract import validate_selection
from .server.real_daily_prefix_schema import CAPTURE_VERSION
from .server.real_daily_source_handles import _directory_chain, identity, parse_json, read_managed
from .storage import digest, json_text

VERSION = 'real-daily-capture-plan-v1'
SPEC_VERSION = 'real-daily-capture-plan-spec-v1'
MAX_PLAN_BYTES = 4 * 1024 * 1024
SET_KEYS = frozenset('assets calendar db_mutations evaluation_performed execution_allowed expected_sessions experiment_test_partition_created manifests market_originals_hashed market_originals_opened missing_inputs model_calls network_requests numeric_market_values_converted pending_source_facts pinned_metadata_authenticates_source_or_venue requested_range schema_version scope_automatically_shrunk selected set_version source_admission status successful_requested_assets'.split())
SELECTED_KEYS = frozenset('as_of_provider_date asset calendar_timezone_labels_compatible in_scope_completed_dates manifest missing_expected_sessions original_byte_identity_verified original_status provider_completed_record_count provider_timezone raw retrieved_at_utc status'.split())
MANIFEST_KEYS = frozenset({'acquisition_version', 'attempt_count', 'batch_status', 'path', 'sha256', 'size_bytes'})
CALENDAR_KEYS = frozenset('authentication evidence_kind exception_count exception_coverage exchange path sha256 size_bytes source_urls timezone'.split())
SPEC_KEYS = frozenset({'schema_version', 'spec_version', 'metadata_set', 'selection', 'capture_root',
                       'capture_id', 'metadata_files', 'source_facts', 'ledger', 'cash_annotations'})
PLAN_KEYS = frozenset({'schema_version', 'plan_version', 'capture_root', 'capture_id', 'metadata_set',
    'metadata_files', 'capture_packet', 'originals_declared', 'resource_limits', 'qualification',
    'plan_digest'})


class RealDailyCapturePlanError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def need(condition, code):
    if not condition:
        raise RealDailyCapturePlanError(code)


def _closed(value, keys, code):
    need(type(value) is dict and set(value) == keys, code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def metadata_locator(value):
    """Refuse raw/secret roles before any filesystem interaction."""
    path_literal(value)
    parts = value.split('/')[1:]
    need(not any(part.casefold() in ('raw', 'keys', 'secrets', 'credentials') for part in parts),
         'metadata_role_required')
    basename = parts[-1].casefold()
    need(basename.endswith('.json') and 'raw' not in basename.rsplit('.', 1)[0] and
         not basename.endswith(('.key.json', '.secret.json')), 'metadata_role_required')
    return value


def _metadata_name(name):
    need(type(name) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json', name) is not None and
         'raw' not in name.casefold() and name not in ('capture.json', 'capture-plan.json'),
         'invalid_metadata_name')
    return name


def _utc(value):
    need(type(value) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value)
         is not None, 'acquisition_time_required')
    try:
        need(datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() == timedelta(0),
             'acquisition_time_required')
    except ValueError:
        raise RealDailyCapturePlanError('acquisition_time_required') from None


def _metadata_set(value, selection):
    value = snapshot(value)
    _closed(value, SET_KEYS, 'invalid_metadata_set')
    need(type(value['schema_version']) is int and value['schema_version'] == 1 and
         value['set_version'] == 'alpha-vantage-metadata-set-v1' and
         value['status'] == 'metadata_complete_pending_admission' and value['source_admission'] == 'pending',
         'metadata_set_not_complete')
    need(value['assets'] == selection['assets'] and value['expected_sessions'] == selection['sessions'] and
         value['missing_inputs'] == [] and type(value['successful_requested_assets']) is int and
         value['successful_requested_assets'] == len(selection['assets']), 'metadata_grid_mismatch')
    _closed(value['requested_range'], {'start_date', 'end_date'}, 'invalid_metadata_set')
    need(value['requested_range'] == {'start_date': selection['sessions'][0], 'end_date': selection['sessions'][-1]},
         'metadata_grid_mismatch')
    for name in ('evaluation_performed', 'execution_allowed', 'experiment_test_partition_created',
                 'numeric_market_values_converted', 'pinned_metadata_authenticates_source_or_venue',
                 'scope_automatically_shrunk'):
        need(value[name] is False, 'metadata_set_authority_refused')
    need(type(value['selected']) is list and len(value['selected']) == len(selection['assets']),
         'metadata_grid_mismatch')
    seen_assets, seen_paths, seen_pins = set(), set(), set()
    manifest_pins = set()
    _closed(value['calendar'], CALENDAR_KEYS, 'invalid_metadata_set')
    metadata_locator(value['calendar']['path']); pin(value['calendar']['sha256'])
    need(type(value['calendar']['size_bytes']) is int and 0 < value['calendar']['size_bytes'] <= MAX_METADATA,
         'invalid_metadata_set')
    need(type(value['manifests']) is list and bool(value['manifests']), 'invalid_metadata_set')
    for item in value['manifests']:
        _closed(item, MANIFEST_KEYS, 'invalid_metadata_set')
        metadata_locator(item['path']); pin(item['sha256'])
        need(type(item['size_bytes']) is int and 0 < item['size_bytes'] <= MAX_METADATA,
             'invalid_metadata_set')
        manifest_pins.add((item['path'], item['sha256'], item['size_bytes']))
    for item in value['selected']:
        _closed(item, SELECTED_KEYS, 'invalid_selected_metadata')
        asset = item['asset']
        need(asset in selection['assets'] and asset not in seen_assets and item['status'] == 'metadata_complete' and
             item['in_scope_completed_dates'] == selection['sessions'] and item['missing_expected_sessions'] == [],
             'metadata_grid_mismatch')
        seen_assets.add(asset)
        _closed(item['manifest'], {'path', 'sha256', 'size_bytes'}, 'invalid_selected_metadata')
        manifest = item['manifest']
        need(type(manifest['size_bytes']) is int and
             (manifest['path'], manifest['sha256'], manifest['size_bytes']) in manifest_pins,
             'manifest_binding_mismatch')
        raw = item['raw']
        _closed(raw, {'path', 'relative_path', 'sha256', 'size_bytes'}, 'invalid_raw_declaration')
        path_literal(raw['path']); pin(raw['sha256'])
        need(type(raw['size_bytes']) is int and 0 < raw['size_bytes'] <= 2 * 1024 * 1024 and
             type(raw['relative_path']) is str and '/' not in raw['relative_path'] and
             raw['relative_path'].endswith('-' + asset + '.raw.json') and
             raw['path'] == manifest['path'].rsplit('/', 1)[0] + '/' + raw['relative_path'],
             'original_locator_binding_mismatch')
        need(raw['path'] not in seen_paths and raw['sha256'] not in seen_pins, 'original_role_alias')
        seen_paths.add(raw['path']); seen_pins.add(raw['sha256'])
        _utc(item['retrieved_at_utc'])
        need(type(item['provider_completed_record_count']) is int and
             item['provider_completed_record_count'] == len(selection['sessions']), 'metadata_grid_mismatch')
        need(item['original_byte_identity_verified'] is False and
             item['calendar_timezone_labels_compatible'] is True and item['provider_timezone'] == 'US/Eastern' and
             item['original_status'] == 'present_size_match', 'metadata_set_authority_refused')
    need(seen_assets == set(selection['assets']), 'metadata_grid_mismatch')
    return value


def build_capture_plan(metadata_set, *, selection, capture_root, capture_id, metadata_files,
                       source_facts, ledger, cash_annotations):
    """Pure closed bridge; copies declarations without inventing source facts."""
    selected = validate_selection(snapshot(selection))
    path_literal(capture_root); identity(capture_id)
    mset = _metadata_set(metadata_set, selected)
    need(type(metadata_files) is dict and 1 <= len(metadata_files) <= 48 and
         all(type(name) is str for name in metadata_files), 'metadata_files_required')
    base = capture_root + '/real_daily_sources/' + capture_id + '/'
    raw_pins = {item['raw']['sha256'] for item in mset['selected']}
    file_records, metadata_pins = [], []
    for name, raw in sorted(metadata_files.items()):
        _metadata_name(name)
        need(type(raw) is bytes and 0 < len(raw) <= MAX_METADATA, 'bounded_metadata_required')
        parse_json(raw)
        need(_sha(raw) not in raw_pins, 'copied_raw_role_alias')
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            raise RealDailyCapturePlanError('invalid_metadata_encoding') from None
        file_records.append({'name': name, 'sha256': _sha(raw), 'size_bytes': len(raw), 'utf8': text})
        metadata_pins.append({'path': base + name, 'sha256': _sha(raw)})
    request = parse_main_assessment_request({
        'schema_version': 1, 'selection': selected, 'ledger': ledger, 'cash_annotations': cash_annotations,
        'source_facts': source_facts, 'method_digest': method_digest(),
        'input_pins': {'origin': 'real_source_declaration', 'metadata': metadata_pins,
                      'raw': [{'asset': item['asset'], 'path': base + 'raw/' + item['asset'] + '.raw.json',
                               'sha256': item['raw']['sha256'], 'size_bytes': item['raw']['size_bytes']}
                              for item in sorted(mset['selected'], key=lambda value: value['asset'])], 'code': []},
    })
    blockers = []
    evidence_pins = {item['sha256'] for item in metadata_pins}
    for name in FACTS:
        fact = request['source_facts'][name]
        if fact['status'] == 'supported':
            need(fact['evidence_sha256'] in evidence_pins, 'unbound_source_evidence')
        else:
            blockers.append({'code': 'source_fact_' + fact['status'], 'fact': name})
    event_review = assess_events(request['ledger'], selection=selected, cash_annotations=request['cash_annotations'])
    blockers.extend(deepcopy(event_review['blockers']))
    originals = [{'asset': item['asset'], 'path': item['raw']['path'], 'sha256': item['raw']['sha256'],
                  'size_bytes': item['raw']['size_bytes'], 'manifest': item['manifest'],
                  'retrieved_at_utc': item['retrieved_at_utc'],
                  'destination': base + 'raw/' + item['asset'] + '.raw.json'}
                 for item in sorted(mset['selected'], key=lambda value: value['asset'])]
    value = {
        'schema_version': 1, 'plan_version': VERSION, 'capture_root': capture_root, 'capture_id': capture_id,
        'metadata_set': mset, 'metadata_files': file_records,
        'capture_packet': {'schema_version': 1, 'capture_version': CAPTURE_VERSION,
                           'capture_id': capture_id, 'request': request},
        'originals_declared': originals,
        'resource_limits': {'import_original_opens': len(originals),
            'import_original_whole_byte_hashes': len(originals),
            'import_original_bytes': sum(item['size_bytes'] for item in originals),
            'import_numeric_values': 0, 'normalization_invocations': 1,
            'normalization_managed_raw_opens': len(originals),
            'normalization_managed_whole_byte_hashes': len(originals),
            'normalization_managed_raw_bytes': sum(item['size_bytes'] for item in originals),
            'normalization_development_ohlc_values': len(selected['assets']) * len(selected['development_sessions']) * 4,
            'final_numeric_values': 0, 'volume_numeric_values': 0, 'worker_invocations': 1,
            'scalar_invocations': 1, 'result_reviews': 1, 'market_requests': 0, 'model_calls': 0,
            'qualified_execution_budget': False},
        'qualification': {'status': 'blocked' if blockers else 'metadata_complete_pending_host_review_and_import',
            'blockers': blockers, 'event_review': event_review,
            'source_fact_counts': {status: sum(fact['status'] == status for fact in request['source_facts'].values())
                                   for status in ('supported', 'unknown', 'rejected')},
            'raw_identity': 'declared_not_raw_verified', 'raw_materialized': False,
            'source_authenticated': False, 'real_source_ready': False, 'execution_allowed': False,
            'final_numeric_access_allowed': False, 'point_in_time': 'not_certified',
            'real_raw_opens': 0, 'real_raw_stats': 0, 'real_raw_hashes': 0, 'numeric_values_decoded': 0,
            'bridge': 'Existing capture request only; separate physical import and original host approval/reservation required.'},
    }
    need(len(json_text(value).encode()) <= MAX_PLAN_BYTES, 'plan_size_limit')
    value['plan_digest'] = digest(value)
    return value


def validate_capture_plan(value):
    _closed(value, PLAN_KEYS, 'invalid_capture_plan')
    need(type(value['schema_version']) is int and value['schema_version'] == 1 and value['plan_version'] == VERSION,
         'invalid_capture_plan')
    packet = value['capture_packet']
    _closed(packet, {'schema_version', 'capture_version', 'capture_id', 'request'}, 'invalid_capture_packet')
    request = parse_main_assessment_request(packet['request'])
    files = {}
    need(type(value['metadata_files']) is list, 'invalid_capture_plan')
    for item in value['metadata_files']:
        _closed(item, {'name', 'sha256', 'size_bytes', 'utf8'}, 'invalid_metadata_file')
        need(type(item['utf8']) is str and item['name'] not in files, 'invalid_metadata_file')
        raw = item['utf8'].encode()
        need(_sha(raw) == item['sha256'] and len(raw) == item['size_bytes'], 'metadata_file_identity_drift')
        files[item['name']] = raw
    rebuilt = build_capture_plan(value['metadata_set'], selection=request['selection'],
        capture_root=value['capture_root'], capture_id=value['capture_id'], metadata_files=files,
        source_facts=request['source_facts'], ledger=request['ledger'], cash_annotations=request['cash_annotations'])
    need(json_text(value) == json_text(rebuilt), 'capture_plan_drift')
    return rebuilt


def read_metadata(path, expected_sha256):
    metadata_locator(path); pin(expected_sha256)
    target = Path(path)
    raw, _ = read_managed(target.parent, target.name, maximum=MAX_METADATA, expected_sha256=expected_sha256)
    parse_json(raw)
    return raw


def load_plan_spec(spec):
    """Read only pinned metadata roles, then construct the pure bridge."""
    _closed(spec, SPEC_KEYS, 'invalid_plan_spec')
    need(type(spec['schema_version']) is int and spec['schema_version'] == 1 and
         spec['spec_version'] == SPEC_VERSION, 'invalid_plan_spec')
    _closed(spec['metadata_set'], {'path', 'sha256'}, 'invalid_metadata_descriptor')
    mset = parse_json(read_metadata(spec['metadata_set']['path'], spec['metadata_set']['sha256']))
    mset = _metadata_set(mset, validate_selection(snapshot(spec['selection'])))
    declared_raw = {item['raw']['sha256'] for item in mset['selected']}
    need(type(spec['metadata_files']) is list and 1 <= len(spec['metadata_files']) <= 48,
         'metadata_files_required')
    files = {}; seen_paths = {spec['metadata_set']['path']}
    for descriptor in spec['metadata_files']:
        _closed(descriptor, {'name', 'path', 'sha256'}, 'invalid_metadata_descriptor')
        _metadata_name(descriptor['name']); metadata_locator(descriptor['path']); pin(descriptor['sha256'])
        need(descriptor['name'] not in files and descriptor['path'] not in seen_paths and
             descriptor['sha256'] not in declared_raw, 'metadata_role_alias')
        seen_paths.add(descriptor['path'])
        files[descriptor['name']] = read_metadata(descriptor['path'], descriptor['sha256'])
    return build_capture_plan(mset, selection=spec['selection'], capture_root=spec['capture_root'],
        capture_id=spec['capture_id'], metadata_files=files, source_facts=spec['source_facts'],
        ledger=spec['ledger'], cash_annotations=spec['cash_annotations'])


def write_plan(plan, out):
    """Write one fresh review artifact directory, without a raw directory."""
    checked = validate_capture_plan(plan)
    path_literal(out)
    capture_base = checked['capture_root'] + '/real_daily_sources/' + checked['capture_id']
    need(not (out == capture_base or out.startswith(capture_base + '/')), 'review_output_must_be_separate')
    target = Path(out)
    parent = directory = None
    try:
        parent, _ = _directory_chain(target.parent, ())
        os.mkdir(target.name, mode=0o700, dir_fd=parent)
        directory = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        files = {'capture-plan.json': json_text(checked).encode(),
                 'capture.json': json_text(checked['capture_packet']).encode()}
        files.update({item['name']: item['utf8'].encode() for item in checked['metadata_files']})
        for name, raw in files.items():
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=directory)
            try:
                offset = 0
                while offset < len(raw):
                    written = os.write(fd, raw[offset:]); need(written > 0, 'metadata_write_failed'); offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
        os.fsync(directory)
    except OSError:
        raise RealDailyCapturePlanError('review_output_failed') from None
    finally:
        for fd in (directory, parent):
            if fd is not None:
                os.close(fd)
    return {'schema_version': 1, 'plan_version': VERSION, 'plan_digest': checked['plan_digest'],
            'status': checked['qualification']['status'], 'execution_allowed': False,
            'raw_materialized': False, 'real_raw_opens': 0, 'real_raw_stats': 0,
            'real_raw_hashes': 0, 'numeric_values_decoded': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare a metadata-only real DAILY capture review plan.')
    parser.add_argument('operation', choices=['plan'])
    parser.add_argument('--spec', required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        spec = parse_json(read_metadata(args.spec, args.expected_sha256))
        summary = write_plan(load_plan_spec(spec), args.out)
    except ValueError as error:
        print(json_text({'status': 'rejected', 'code': getattr(error, 'code', 'invalid_metadata')}), file=sys.stderr)
        return 2
    print(json_text(summary))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
