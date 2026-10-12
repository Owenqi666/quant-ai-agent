"""Record static or loopback-only CLI qualification without model inference.

Exit 0 means diagnostics were successfully collected, not permission to dispatch.
Exit 2 means a required check failed or could not be observed. Never overwrites a
previous run, edits provider configuration, or creates a research workspace.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_alpha.server.provider_cli_profile import build_profile, SUPPORTED_CONTRACT_IDS
from paper_alpha.server.provider_cli_probe import probe_local_cli
from paper_alpha.server.provider_qualification import (
    assess_qualification, verify_evidence, write_evidence,
)
from paper_alpha.server.provider_schema import CONTRACT_ID, ProviderConfig


def save(path: Path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


async def check(config: ProviderConfig, output_dir: Path, *, probe=False,
                evidence_dir: Path | None = None, max_seconds=20,
                contract_id=CONTRACT_ID) -> tuple[dict, int]:
    # Refuse linked output parents before resolving paths; a new directory is
    # essential because failures and incompatible observations are evidence too.
    output_dir = output_dir.absolute()
    if any(path.is_symlink() for path in (output_dir, *output_dir.parents)):
        raise ValueError('Output directory must not contain symbolic links')
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {'check_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
              'operation_status': 'failed', 'real_model_invocations': 0,
              'real_dispatch_enabled': False, 'qualification': None,
              'mode': 'local_cli_probe' if probe else 'static',
              'error': None, 'source_sha256': {}, 'contract_id': contract_id}
    exit_code = 2
    try:
        profile = build_profile(config, contract_id=contract_id)
        save(output_dir / 'profile.json', profile)
        verified = None
        if evidence_dir is not None:
            verified = verify_evidence(evidence_dir, profile)
        if probe and profile['static_checks']['passed']:
            observation = await probe_local_cli(config, output_dir / 'probe', max_seconds=max_seconds,
                                                contract_id=contract_id)
            verified = write_evidence(output_dir / 'evidence', profile, observation)
        qualification = assess_qualification(profile, verified)
        save(output_dir / 'qualification.json', qualification)
        if verified is not None:
            save(output_dir / 'evidence-verification.json', verified)
        result['qualification'] = qualification
        check_succeeded = profile['static_checks']['passed']
        if verified is not None:
            check_succeeded = check_succeeded and verified['valid']
        if probe:
            observation = verified['observation'] if verified and verified['valid'] else None
            check_succeeded = bool(check_succeeded and observation
                                   and observation['request_observed'] and observation['process_stopped'])
        result['operation_status'] = 'completed' if check_succeeded else 'incomplete'
        exit_code = 0 if check_succeeded else 2
    except Exception as exc:
        # No CLI stderr, environment, request bodies, or arbitrary exception
        # messages are copied to a public readiness summary.
        result['error'] = {'code': 'READINESS_CHECK_FAILED', 'type': type(exc).__name__}
    finally:
        files = [Path(__file__), *sorted((ROOT / 'paper_alpha/server').glob('provider_*.py')),
                 ROOT / 'paper_alpha/server/codex_cli_provider.py']
        result['source_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in files if p.is_file()}
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        result['exit_code'] = exit_code
        save(output_dir / 'result.json', result)
    return result, exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True, help='Exact installed CLI path or PATH command')
    parser.add_argument('--model', required=True, help='Requested identity; does not assert account/model availability')
    parser.add_argument('--expected-cli-version', default='0.145.0')
    parser.add_argument('--reasoning-effort', choices=('low', 'medium', 'high', 'xhigh'), default='low')
    parser.add_argument('--contract-id', choices=SUPPORTED_CONTRACT_IDS, default=CONTRACT_ID,
                        help='Business contract to inspect; qualification never authorizes real inference')
    parser.add_argument('--output-dir', required=True, type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--probe-local-cli', action='store_true')
    action.add_argument('--verify-evidence', type=Path)
    parser.add_argument('--max-seconds', type=int, default=20)
    args = parser.parse_args(argv)
    if not 1 <= args.max_seconds <= 20:
        parser.error('max-seconds must be in 1..20')
    try:
        config = ProviderConfig(config_id='readiness-v1' if args.contract_id == CONTRACT_ID else 'readiness-v2',
            mode='codex_cli', enabled=True,
            executable=args.executable, model=args.model, reasoning_effort=args.reasoning_effort,
            expected_cli_version=args.expected_cli_version)
        result, code = asyncio.run(check(config, args.output_dir, probe=args.probe_local_cli,
                                        evidence_dir=args.verify_evidence, max_seconds=args.max_seconds,
                                        contract_id=args.contract_id))
    except (OSError, ValueError) as exc:
        print(json.dumps({'operation_status': 'not_started', 'error_type': type(exc).__name__,
                          'real_model_invocations': 0, 'real_dispatch_enabled': False}))
        return 2
    qualification = result['qualification'] or {}
    print(json.dumps({'operation_status': result['operation_status'], 'output_dir': str(args.output_dir),
        'real_model_invocations': 0, 'real_dispatch_enabled': False,
        'blockers': [item['code'] for item in qualification.get('blockers', [])],
        'error': result['error']}, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
