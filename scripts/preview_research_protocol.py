"""Freeze or independently replay a controlled-fixture monthly signal diagnostic."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha import research_protocol as core
from paper_alpha.storage import atomic_json, read_json

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
SCOPE = 'Controlled-fixture signal and coverage diagnostics. No portfolio or market-performance claim.'


def load_bounded(path, limit=core.MAX_BUNDLE_BYTES):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('Input exceeds the controlled-fixture file limit')
    return read_json(path)


def sha(path):
    if Path(path).stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError('Snapshot exceeds the file limit')
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(out, config, target_month, bundle=None):
    config = core.validate_config(config)
    bundle = core.demo_bundle(target_month) if bundle is None else bundle
    report = core.preview(config, target_month, bundle)
    values = {'config.json': config, 'input.json': bundle, 'report.json': report}
    encoded = {name: (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
               for name, value in values.items()}
    if any(len(value) > MAX_SNAPSHOT_BYTES for value in encoded.values()):
        raise ValueError('Diagnostic snapshot exceeds the file limit')
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    for name, value in encoded.items():
        (out / name).write_bytes(value)
    preset = next(item for item in core.presets() if item['config']['mode'] == config['mode'])
    manifest = {'schema_version': 1, 'semantics_version': core.SEMANTICS_VERSION,
                'target_month': target_month, 'core_sha256': sha(core.__file__),
                'files': {name: sha(out / name) for name in values},
                'sources': preset['sources'], 'unresolved': preset['unresolved'],
                'scope': SCOPE}
    atomic_json(out / 'manifest.json', manifest)
    return verify(out)


def verify(out):
    out = Path(out).resolve()
    manifest = load_bounded(out / 'manifest.json')
    names = {'config.json', 'input.json', 'report.json'}
    if (manifest.get('schema_version') != 1 or manifest.get('semantics_version') != core.SEMANTICS_VERSION
            or manifest.get('core_sha256') != sha(core.__file__) or set(manifest.get('files', {})) != names):
        raise ValueError('Unsupported or changed diagnostic manifest / computation code')
    for name in names:
        if sha(out / name) != manifest['files'][name]:
            raise ValueError('Diagnostic snapshot digest mismatch: ' + name)
    config, bundle = load_bounded(out / 'config.json'), load_bounded(out / 'input.json')
    preset = next(item for item in core.presets() if item['config']['mode'] == core.validate_config(config)['mode'])
    if manifest.get('sources') != preset['sources'] or manifest.get('unresolved') != preset['unresolved'] or manifest.get('scope') != SCOPE:
        raise ValueError('Diagnostic source declarations differ from the bound computation version')
    saved = load_bounded(out / 'report.json', MAX_SNAPSHOT_BYTES)
    if core.preview(config, manifest['target_month'], bundle) != saved:
        raise ValueError('Recomputed diagnostic differs from its saved report')
    return {'verified': True, 'status': saved['status'], 'config_digest': saved['config_digest'],
            'input_digest': saved['input_digest'], 'directory': str(out)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--preset', choices=('paper', 'project'))
    source.add_argument('--config', type=Path)
    parser.add_argument('--target-month')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    if args.verify:
        if any((args.preset, args.config, args.target_month, args.bundle, args.out)):
            parser.error('--verify cannot be combined with creation arguments')
        result = verify(args.verify)
    else:
        if not args.out or not args.target_month:
            parser.error('Creation requires --out and --target-month')
        config = (load_bounded(args.config) if args.config else
                  next(p['config'] for p in core.presets() if p['config']['mode'] == (args.preset or 'project')))
        result = freeze(args.out, config, args.target_month, load_bounded(args.bundle) if args.bundle else None)
    print(result)


if __name__ == '__main__':
    main()
