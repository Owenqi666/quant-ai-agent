#!/usr/bin/env python3
"""H0 metadata administration; a final numerical executor is not available.

freeze binds one or two evaluated candidates to an actual verified, completed
typed development attempt. permission is an explicit local operation; current
real sources remain unqualified, even with a human receipt. inspect reads only
frozen metadata and permanent exposure counts, without migration or final
prices. This tool never reserves/consumes a final read, evaluates final metrics,
launches a model, or starts a service. H1 needs a separate qualified executor.
"""
from pathlib import Path
from types import SimpleNamespace
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha.server.daily_holdout import freeze_candidates, create_holdout_permission, holdout_status
from paper_alpha.server.provider_contract import strict_json
from paper_alpha.server.service import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['freeze', 'permission', 'inspect'])
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--freeze-id')
    args = parser.parse_args(argv)
    if not args.workspace.is_absolute() or not (args.workspace / 'workbench.sqlite3').is_file():
        parser.error('Use an existing initialized absolute workspace')
    if args.action == 'inspect':
        if not args.freeze_id:
            parser.error('inspect requires --freeze-id')
        store = SimpleNamespace(root=args.workspace, db_path=args.workspace / 'workbench.sqlite3')
        value = holdout_status(store, args.freeze_id)
    else:
        if args.spec is None:
            parser.error('freeze/permission requires --spec')
        spec = strict_json(args.spec.read_bytes(), max_bytes=65536)
        store = Store(args.workspace)
        if args.action == 'freeze':
            record = freeze_candidates(store, **spec)
            value = {'freeze_id': record['id'], 'candidate_policy_digest': record['candidate_policy_digest'],
                     'development_state_digest': record['development_state_digest'],
                     'status': holdout_status(store, record['id'])}
        else:
            record = create_holdout_permission(store, **spec)
            value = {'permission_id': record['id'], 'freeze_id': record['freeze_id'],
                     'authorization_kind': record['request']['authorization_kind'], 'deadline_at': record['deadline_at'],
                     'numeric_rows_read_by_this_tool': 0, 'final_executor_available': False}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
