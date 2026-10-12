#!/usr/bin/env python3
"""Prepare one bounded original Native task without dispatching any proposal.

The authorization record must contain the original scoped human message. Its
digest records provenance; this CLI's printed summary is not payment authority.
Only the host may select a catalog or qualification report. HTTP only attaches.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha.server.native_correction_start import prepare_native_start, _directory, _read_file, _atomic
from paper_alpha.server.native_correction_start_schema import NativeCorrectionPrepare
from paper_alpha.server.provider_runtime import load_provider_registry
from paper_alpha.server.service import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--provider-config', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--spec', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--allow-fixture', action='store_true')
    modes.add_argument('--allow-real', action='store_true')
    parser.add_argument('--authorization-record', type=Path,
                        help='Bounded JSON containing record_kind=original_user_message and original_message')
    parser.add_argument('--read-qualification', type=Path,
                        help='Existing matching synthetic full-matrix boundary report; never runs a probe')
    args = parser.parse_args(argv)
    if not args.workspace.is_absolute() or not (args.workspace / 'workbench.sqlite3').is_file():
        parser.error('Use an existing initialized absolute workspace')
    if args.allow_real and args.authorization_record is None:
        parser.error('Real preparation requires the original scoped user authorization record')
    store, spec = None, None
    try:
        spec, _ = _read_file(_directory(args.spec.parent), args.spec.name)
        spec = NativeCorrectionPrepare.model_validate(spec).model_dump()
        authorization_digest = None
        if args.authorization_record is not None:
            value, _ = _read_file(_directory(args.authorization_record.parent), args.authorization_record.name)
            if (type(value) is not dict or set(value) != {'schema_version', 'record_kind', 'original_message'}
                    or type(value['schema_version']) is not int or value['schema_version'] != 1
                    or value['record_kind'] != 'original_user_message'
                    or type(value['original_message']) is not str or not value['original_message'].strip()
                    or len(value['original_message']) > 32000):
                raise ValueError('Authorization record must preserve the original user message')
            authorization_digest = hashlib.sha256(value['original_message'].encode()).hexdigest()
        registry = load_provider_registry(args.provider_config, allow_fixture=args.allow_fixture, allow_real=args.allow_real)
        store = Store(args.workspace)
        result = prepare_native_start(store, registry, spec, args.catalog,
            human_authorization_digest=authorization_digest, read_qualification=args.read_qualification)
    except (ValueError, OSError) as exc:
        code = getattr(exc, 'code', 'NATIVE_PREPARATION_FAILED')
        original = {'session_ids': [], 'grant_ids': [], 'loop_ids': []}
        if store is not None and spec is not None:
            program = spec['program_id']
            original = {
                'session_ids': [r['id'] for r in store._read('SELECT id FROM research_sessions WHERE idempotency_key=?', (program + '-session',))],
                'grant_ids': [r['id'] for r in store._read('SELECT id FROM provider_loop_native_grants WHERE idempotency_key=?', (program + '-grant',))],
                'loop_ids': [r['id'] for r in store._read('SELECT id FROM research_loops WHERE idempotency_key=?', (program + '-loop',))]}
            folder = store.root / 'native-start-preparations'
            folder.mkdir(exist_ok=True)
            _atomic(_directory(folder), program + '.json', {'schema_version': 1, 'status': 'blocked',
                'reason_code': code, 'catalog_publication': 'not_performed_by_failed_attempt', 'original_records': original,
                'record_is_authority': False, 'model_invocations': 0})
        print(json.dumps({'schema_version': 1, 'status': 'blocked', 'reason_code': code,
            'catalog_publication': 'not_performed_by_failed_attempt', 'original_records': original, 'model_invocations': 0}, indent=2))
        return 1
    print(json.dumps({'schema_version': 1, 'prepared': result, 'model_invocations': 0,
        'authorization_provenance': {'kind': 'original_user_message' if authorization_digest else 'fixture_only',
            'digest': authorization_digest, 'printed_summary_is_dispatch_authorization': False}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
