"""Independently verify bounded bundle contents and optionally installed sources."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.semantic_evaluation import MAX_BYTES, verify_bundle


def read_bundle(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Use a regular semantic JSON bundle no larger than 16 MiB')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Semantic bundle exceeds 16 MiB')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate JSON key in semantic bundle')
            value[key] = item
        return value
    def invalid_constant(value):
        raise ValueError('Nonfinite JSON constant is forbidden: ' + value)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=invalid_constant)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--source-root', type=Path, default=ROOT,
                        help='Explicit installed source bundle for all four fixed source hashes (default: this package).')
    parser.add_argument('--snapshot-only', action='store_true',
                        help='Verify included UTF-8 snapshots/digests only; omitted PDF bytes are not reverified.')
    args = parser.parse_args()
    try:
        result = verify_bundle(read_bundle(args.bundle), root=None if args.snapshot_only else args.source_root)
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({'passed': False, 'error_type': type(exc).__name__, 'error': str(exc)}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
