"""Check declaration readiness offline; write a new immutable report directory."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.research_readiness import InputRoot, MAX_MANIFEST_BYTES, canonical, check_readiness, freeze_digest, strict_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path, help='Input directory; all manifest/artifact paths are relative to it')
    parser.add_argument('--manifest', required=True, help='Relative strict JSON manifest path')
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument('--out', type=Path, help='NEW directory outside the input root')
    output.add_argument('--freeze-digest', action='store_true', help='Print the declaration digest only; this does not freeze or approve a study')
    parser.add_argument('--as-of', help='Explicit timezone-aware check reference time; required for heldout completion')
    args = parser.parse_args(argv)
    try:
        if args.freeze_digest:
            with InputRoot(args.root) as root:
                _, raw = root.read(args.manifest, limit=MAX_MANIFEST_BYTES, retain=True)
                manifest = strict_json(raw)
                if not isinstance(manifest, dict):
                    raise ValueError('Manifest must be an object')
            print(freeze_digest(manifest))
            return 0
        destination = args.out.absolute()
        if destination.resolve().is_relative_to(args.root.resolve()):
            raise ValueError('Output must be outside the inspected input root')
        # Do not create parents or overwrite an earlier successful/failed attempt.
        with InputRoot(destination.parent) as parent:
            os.mkdir(destination.name, 0o700, dir_fd=parent.fd)
            directory = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
        try:
            report = check_readiness(args.root, args.manifest, as_of=args.as_of)
            payload = canonical(report)
            descriptor = os.open('report.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)
        print(json.dumps({'status': report['status'], 'report': str(destination / 'report.json'),
                          'report_sha256': report['report_sha256'], 'reason_count': len(report['reasons'])}))
        return 2 if report['status'] == 'blocked' else 0
    except (OSError, ValueError, TypeError, RecursionError):
        print('Readiness request refused; use safe inputs and a new output directory. No execution was enabled.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
