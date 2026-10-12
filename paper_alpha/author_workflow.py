"""Frozen source-specific diagnostics. CLI: audit / prepare / verify."""
from __future__ import annotations

import argparse
from importlib.metadata import version
from pathlib import Path, PurePosixPath
import shutil
import stat

from . import author_panel, author_reference
from .author_archive_contract import MAX_PANEL_BYTES, SEMANTICS_VERSION
from .author_panel_schema import AuthorReference
from .evidence import sha256
from .storage import atomic_json, digest, json_text, read_json
from .workflow import environment, code_files

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SOURCE_FILES = ['paper_alpha/' + name + '.py' for name in (
    'author_panel', 'author_panel_schema', 'author_reference', 'author_workflow',
    'author_archive', 'author_archive_contract', 'storage', 'evidence', 'workflow')]
REQUIRED_SOURCE_FILES += ['requirements-lock.txt', 'requirements-web-lock.txt', 'pyproject.toml']
SOURCE_FILES = sorted(set(code_files()) | {'requirements-web-lock.txt'})
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def _load(path, limit=MAX_ARTIFACT_BYTES):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError('Author artifact must be a bounded independent regular file')
    return read_json(path)


def run(panel, output_dir, *, source_path=None):
    """Freeze normalized input; raw-source claim is earned by exact re-extraction."""
    panel = author_panel.validate_panel(panel)
    raw_verified = False
    if source_path is not None:
        from .author_archive import extract_panel
        actual = extract_panel(source_path, **panel['selection'])
        if json_text(actual) != json_text(panel):
            raise ValueError('Panel differs from verified raw source extraction')
        raw_verified = True
    result = author_panel.evaluate(panel)
    reference = author_reference.check(panel, result)
    if not reference['passed']:
        raise ValueError('Independent author-panel reference rejected the result')
    out = Path(output_dir).absolute()
    if out.resolve() != out:
        raise ValueError('Author output directory may not traverse symlinks')
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / 'input.json', panel)
    atomic_json(out / 'result.json', result)
    atomic_json(out / 'reference.json', reference)
    atomic_json(out / 'source_check.json', {'schema_version': 1, 'raw_file_verified_at_creation': raw_verified,
                'verification_method': 'pinned-hashes-and-exact-row-reextraction' if raw_verified else 'normalized-panel-only',
                'input_digest': digest(panel), 'source': panel['source']})
    runtime = environment()
    runtime['packages'].update({name: version(name) for name in ('pydantic', 'h5py')})
    atomic_json(out / 'environment.json', runtime)
    for name in SOURCE_FILES:
        path = ROOT / name
        before = sha256(path)
        target = out / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha256(target) != before or sha256(path) != before:
            raise ValueError('Diagnostic source changed during snapshotting')
    (out / 'report.md').write_text(author_panel.render_report(result), encoding='utf-8')
    files = {str(path.relative_to(out)): sha256(path) for path in sorted(out.rglob('*')) if path.is_file()}
    atomic_json(out / 'manifest.json', {'schema_version': 1, 'semantics_version': SEMANTICS_VERSION,
                                      'input_digest': digest(panel), 'result_digest': digest(result), 'files': files})
    return verify(out)


def verify(output_dir, *, source_path=None):
    out = Path(output_dir).absolute()
    if out.resolve() != out or not out.is_dir():
        raise ValueError('Author output path is missing or unsafe')
    manifest = _load(out / 'manifest.json', 1024 * 1024)
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'semantics_version', 'input_digest', 'result_digest', 'files'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or manifest['semantics_version'] != SEMANTICS_VERSION):
        raise ValueError('Unsupported author output manifest')
    expected = {'input.json', 'result.json', 'reference.json', 'source_check.json', 'environment.json', 'report.md'}
    expected.update('source/' + name for name in REQUIRED_SOURCE_FILES)
    files = manifest['files']
    if not isinstance(files, dict) or not expected <= set(files) or len(files) > 4096:
        raise ValueError('Author artifact inventory is incomplete or unsupported')
    if any(name not in expected and not name.startswith('source/') for name in files):
        raise ValueError('Author artifact contains unsupported files')
    expected = set(files)
    actual, total = set(), 0
    for path in out.rglob('*'):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Unsafe author artifact')
        actual.add(str(path.relative_to(out)))
        total += info.st_size
        if len(actual) > len(expected) + 1 or total > MAX_ARTIFACT_BYTES:
            raise ValueError('Author artifact inventory exceeds its bound')
    if actual != expected | {'manifest.json'}:
        raise ValueError('Author artifact inventory changed')
    for name, fingerprint in manifest['files'].items():
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or sha256(out / name) != fingerprint:
            raise ValueError('Author artifact hash mismatch')
    panel = author_panel.validate_panel(_load(out / 'input.json', MAX_PANEL_BYTES))
    result, reference = _load(out / 'result.json'), _load(out / 'reference.json')
    if digest(panel) != manifest['input_digest'] or digest(result) != manifest['result_digest']:
        raise ValueError('Author manifest is not bound to input and result')
    AuthorReference.model_validate(reference)
    checked = author_reference.check(panel, result)
    if not checked['passed'] or json_text(checked) != json_text(reference):
        raise ValueError('Independent author reference verification failed')
    if (out / 'report.md').read_text(encoding='utf-8') != author_panel.render_report(result):
        raise ValueError('Author report does not match the frozen result')
    receipt = _load(out / 'source_check.json')
    if (not isinstance(receipt, dict) or set(receipt) != {'schema_version', 'raw_file_verified_at_creation', 'verification_method', 'input_digest', 'source'}
            or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
            or type(receipt['raw_file_verified_at_creation']) is not bool or receipt['input_digest'] != digest(panel)
            or receipt['source'] != panel['source']
            or receipt['verification_method'] != ('pinned-hashes-and-exact-row-reextraction' if receipt['raw_file_verified_at_creation'] else 'normalized-panel-only')):
        raise ValueError('Author source check does not match the frozen panel')
    raw_verified = False
    if source_path is not None:
        from .author_archive import extract_panel
        actual = extract_panel(source_path, **panel['selection'])
        if json_text(actual) != json_text(panel):
            raise ValueError('Panel differs from the verified raw source')
        raw_verified = True
    return {'verified': True, 'input_digest': digest(panel), 'result_digest': digest(result),
            'reference': checked, 'raw_source_reverified': raw_verified,
            'raw_file_verified_at_creation': receipt['raw_file_verified_at_creation'],
            'summary': result['summary']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('audit')
    inspect.add_argument('--source', type=Path, required=True)
    inspect.add_argument('--out', type=Path, required=True, help='New output directory')
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--source', type=Path, required=True)
    prepare.add_argument('--target-month', required=True)
    prepare.add_argument('--row-offset', type=int, default=0)
    prepare.add_argument('--row-count', type=int, default=128)
    prepare.add_argument('--out', type=Path, required=True, help='New output directory')
    check = commands.add_parser('verify')
    check.add_argument('directory', type=Path)
    check.add_argument('--source', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'audit':
        from .author_archive import audit
        result = audit(args.source)
        args.out.mkdir(parents=True, exist_ok=False)
        atomic_json(args.out / 'audit.json', result)
        print(json_text({'audit': str(args.out / 'audit.json'), 'source': result.get('source')}))
    elif args.command == 'prepare':
        from .author_archive import extract_panel
        panel = extract_panel(args.source, args.target_month, args.row_offset, args.row_count)
        print(json_text(run(panel, args.out, source_path=args.source)))
    else:
        print(json_text(verify(args.directory, source_path=args.source)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
