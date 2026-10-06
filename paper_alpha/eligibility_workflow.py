"""Freeze a predeclared author-data screen; never overwrite a prior attempt."""
from __future__ import annotations

import argparse
from importlib.metadata import version
from pathlib import Path, PurePosixPath
import re
import shutil
import stat

from . import eligibility
from .eligibility_archive import SEMANTICS_VERSION, scan
from .evidence import sha256
from .storage import atomic_json, digest, json_text, read_json
from .workflow import code_files, environment

ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 256 * 1024
MAX_PLAN_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
REQUIRED_SOURCE_FILES = ['paper_alpha/' + name + '.py' for name in (
    '__init__', 'eligibility', 'eligibility_schema', 'eligibility_archive', 'eligibility_workflow',
    'author_archive', 'author_archive_contract', 'author_panel', 'author_panel_schema',
    'storage', 'evidence', 'workflow', 'contracts', 'expressions', 'reporting')]
# workflow.code_files/environment are shared helpers. Importing workflow also
# imports expressions -> vendor.operators; code_files reads its provenance file.
REQUIRED_SOURCE_FILES += ['paper_alpha/vendor/__init__.py', 'paper_alpha/vendor/operators.py',
                          'paper_alpha/vendor/PROVENANCE.json']
REQUIRED_SOURCE_FILES += ['requirements-lock.txt', 'requirements-web-lock.txt', 'pyproject.toml']
ARTIFACTS = {'plan.json', 'input.json', 'result.json', 'source_check.json', 'environment.json', 'report.md'}


def _load(path, limit=MAX_ARTIFACT_BYTES):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError('Eligibility artifact must be a bounded independent regular file')
    return read_json(path)


def _snapshot(out):
    files = {**code_files(), 'requirements-web-lock.txt': ROOT / 'requirements-web-lock.txt'}
    hashes = {}
    for name, path in files.items():
        before = sha256(path)
        target = out / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha256(target) != before or sha256(path) != before:
            raise ValueError('Eligibility source changed while snapshotting')
        hashes[name] = before
    return files, hashes


def run(plan, output_dir, *, source_path):
    """Write the plan before source access; failed attempts retain their error."""
    plan = eligibility.validate_plan(plan)
    out = Path(output_dir).absolute()
    if out.resolve() != out:
        raise ValueError('Eligibility output directory may not traverse symlinks')
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / 'plan.json', plan)
    try:
        runtime = environment()
        runtime['packages'].update({name: version(name) for name in ('pydantic', 'h5py')})
        atomic_json(out / 'environment.json', runtime)
        files, initial_hashes = _snapshot(out)
        value = scan(source_path, plan)
        if json_text(value['plan']) != json_text(plan):
            raise ValueError('Eligibility scan differs from the frozen plan')
        result = eligibility.evaluate(value)
        atomic_json(out / 'input.json', value)
        atomic_json(out / 'result.json', result)
        atomic_json(out / 'source_check.json', {'schema_version': 1,
                    'creation_verification_claim': True,
                    'verification_method': 'pinned-hashes-and-all-original-row-scan',
                    'plan_digest': digest(plan), 'input_digest': digest(value), 'source': value['source']})
        (out / 'report.md').write_text(eligibility.render_report(result), encoding='utf-8')
        if any(sha256(path) != initial_hashes[name] for name, path in files.items()):
            raise ValueError('Eligibility implementation changed during the scan')
        inventory = {str(path.relative_to(out)): sha256(path) for path in sorted(out.rglob('*')) if path.is_file()}
        atomic_json(out / 'manifest.json', {'schema_version': 1, 'semantics_version': SEMANTICS_VERSION,
                    'plan_digest': digest(plan), 'input_digest': digest(value), 'result_digest': digest(result),
                    'files': inventory})
        return verify(out)
    except Exception as exc:
        atomic_json(out / 'error.json', {'schema_version': 1, 'status': 'failed',
                    'plan_digest': digest(plan), 'error_type': type(exc).__name__, 'message': str(exc)[:4000]})
        raise


def verify(output_dir, *, source_path=None):
    """Without MAT, only consistency is checked, including the source claim."""
    out = Path(output_dir).absolute()
    if out.resolve() != out or not out.is_dir():
        raise ValueError('Eligibility output path is missing or unsafe')
    manifest = _load(out / 'manifest.json', 1024 * 1024)
    fields = {'schema_version', 'semantics_version', 'plan_digest', 'input_digest', 'result_digest', 'files'}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or manifest['semantics_version'] != SEMANTICS_VERSION):
        raise ValueError('Unsupported eligibility artifact manifest')
    files = manifest['files']
    required = ARTIFACTS | {'source/' + name for name in REQUIRED_SOURCE_FILES}
    if not isinstance(files, dict) or not required <= set(files) or len(files) > 4096:
        raise ValueError('Eligibility artifact inventory is incomplete or unsupported')
    for name, fingerprint in files.items():
        relative = PurePosixPath(name)
        if (not isinstance(name, str) or relative.is_absolute() or '..' in relative.parts
                or str(relative) != name or name not in ARTIFACTS and not name.startswith('source/')
                or not isinstance(fingerprint, str) or re.fullmatch(r'[0-9a-f]{64}', fingerprint) is None):
            raise ValueError('Eligibility artifact inventory contains unsafe or unsupported entries')
    actual, total = set(), 0
    for path in out.rglob('*'):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe eligibility artifact')
        actual.add(str(path.relative_to(out)))
        total += info.st_size
        if len(actual) > len(files) + 1 or total > MAX_ARTIFACT_BYTES:
            raise ValueError('Eligibility artifact inventory exceeds its bound')
    if actual != set(files) | {'manifest.json'}:
        raise ValueError('Eligibility artifact inventory changed or the attempt failed')
    for name, fingerprint in files.items():
        if sha256(out / name) != fingerprint:
            raise ValueError('Eligibility artifact hash mismatch')
    plan = eligibility.validate_plan(_load(out / 'plan.json', MAX_PLAN_BYTES))
    value = eligibility.validate_scan(_load(out / 'input.json', MAX_SCAN_BYTES))
    result = _load(out / 'result.json', 1024 * 1024)
    if (json_text(value['plan']) != json_text(plan) or digest(plan) != manifest['plan_digest']
            or digest(value) != manifest['input_digest'] or digest(result) != manifest['result_digest']):
        raise ValueError('Eligibility manifest is not bound to its plan and result')
    expected = eligibility.evaluate(value)
    if json_text(expected) != json_text(result):
        raise ValueError('Eligibility result differs from the frozen scan decision')
    if (out / 'report.md').read_text(encoding='utf-8') != eligibility.render_report(expected):
        raise ValueError('Eligibility report differs from the computed decision')
    receipt = _load(out / 'source_check.json', MAX_PLAN_BYTES)
    expected_receipt = {'schema_version': 1, 'creation_verification_claim': True,
                       'verification_method': 'pinned-hashes-and-all-original-row-scan',
                       'plan_digest': digest(plan), 'input_digest': digest(value), 'source': value['source']}
    if json_text(receipt) != json_text(expected_receipt):
        raise ValueError('Eligibility source verification claim differs from the frozen scan')
    raw_verified = False
    if source_path is not None:
        actual_scan = scan(source_path, plan)
        if json_text(actual_scan) != json_text(value):
            raise ValueError('Eligibility aggregate differs from verified all-original-row source recomputation')
        raw_verified = True
    return {'verified': True, 'plan_digest': digest(plan), 'input_digest': digest(value),
            'result_digest': digest(result), 'summary': result['summary'],
            'creation_verification_claim': True, 'raw_source_reverified': raw_verified,
            'verification_scope': 'raw_source_recomputed' if raw_verified else 'aggregate_consistency_only'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('run')
    prepare.add_argument('--plan', type=Path, required=True)
    prepare.add_argument('--source', type=Path, required=True)
    prepare.add_argument('--out', type=Path, required=True)
    check = commands.add_parser('verify')
    check.add_argument('directory', type=Path)
    check.add_argument('--source', type=Path)
    args = parser.parse_args(argv)
    result = (run(_load(args.plan, MAX_PLAN_BYTES), args.out, source_path=args.source)
              if args.command == 'run' else verify(args.directory, source_path=args.source))
    print(json_text(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
