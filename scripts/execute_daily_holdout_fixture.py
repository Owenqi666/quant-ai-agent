#!/usr/bin/env python3
"""Execute/recover/read back one original fixture final attempt; never real data."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha.server.daily_holdout_executor import (execute_fixture_holdout,
    recover_fixture_holdout, verify_fixture_holdout)
from paper_alpha.server.provider_contract import strict_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['execute', 'recover', 'verify'])
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--freeze-id')
    args = parser.parse_args(argv)
    if not args.workspace.is_absolute() or not (args.workspace / 'workbench.sqlite3').is_file():
        parser.error('Existing initialized absolute schema27 workspace required')
    store = SimpleNamespace(root=args.workspace, db_path=args.workspace / 'workbench.sqlite3')
    if args.action == 'execute':
        if args.spec is None or args.freeze_id is not None:
            parser.error('execute requires --spec with exact freeze_id/permission_id/idempotency_key')
        spec = strict_json(args.spec.read_bytes(), max_bytes=65536)
        if type(spec) is not dict or set(spec) != {'freeze_id', 'permission_id', 'idempotency_key'}:
            parser.error('Closed original final execution request required')
        value = execute_fixture_holdout(store, **spec)
    else:
        if not args.freeze_id or args.spec is not None:
            parser.error('recover/verify requires --freeze-id')
        function = recover_fixture_holdout if args.action == 'recover' else verify_fixture_holdout
        value = function(store, args.freeze_id)
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
