"""Build or verify an independent exact research sample from a review bundle."""
import argparse
import json
from pathlib import Path

from paper_alpha.research_evaluation import export_sample, verify_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build')
    build.add_argument('--packet', type=Path, required=True)
    build.add_argument('--out', type=Path, required=True)
    verify = sub.add_parser('verify')
    verify.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = export_sample(args.packet, args.out) if args.command == 'build' else verify_bundle(args.out)
    except Exception as exc:
        print(json.dumps({'passed': False, 'out': str(args.out), 'error': type(exc).__name__ + ': ' + str(exc)[:2000]}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
