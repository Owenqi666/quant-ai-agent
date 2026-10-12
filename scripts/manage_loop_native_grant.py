#!/usr/bin/env python3
"""Local administration of a frozen, at-most-11-slot Native program.

This tool never dispatches a proposal, launches a model, or starts a service.
create requires a closed LoopNativeGrantCreate JSON spec. Each call freezes its
operation, source/parent, effect key, candidate bound, and (for real permission)
instruction digest and repair/decide target call. A real spec also requires its
scoped human authorization digest; older Host grants are never inherited.
inspect reads the existing ledger without Store construction or migration.
revoke is permanent. A second grant cannot reset the same program allowance.
"""
from pathlib import Path
from types import SimpleNamespace
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper_alpha.server.provider_runtime import load_provider_registry
from paper_alpha.server.provider_loop_native_authority import (
    LoopNativeResearchAuthority, create_loop_native_grant, revoke_loop_native_grant)
from paper_alpha.server.provider_contract import strict_json
from paper_alpha.server.service import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'inspect', 'revoke'])
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--provider-config', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--allow-real', action='store_true')
    modes.add_argument('--allow-fixture', action='store_true')
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--grant-id')
    parser.add_argument('--reason')
    args = parser.parse_args(argv)
    if not args.workspace.is_absolute() or not (args.workspace / 'workbench.sqlite3').is_file():
        parser.error('Use an existing initialized absolute workspace')
    if args.action == 'create' and args.spec is None:
        parser.error('create requires --spec')
    if args.action != 'create' and not args.grant_id:
        parser.error('inspect/revoke requires --grant-id')
    if args.action == 'revoke' and not args.reason:
        parser.error('revoke requires --reason')
    registry = load_provider_registry(args.provider_config, allow_real=args.allow_real, allow_fixture=args.allow_fixture)
    if args.action == 'inspect':
        store = SimpleNamespace(root=args.workspace, db_path=args.workspace / 'workbench.sqlite3')
        value = LoopNativeResearchAuthority(store, registry).status(args.grant_id)
    else:
        store = Store(args.workspace)
        if args.action == 'create':
            grant = create_loop_native_grant(store, registry, **strict_json(args.spec.read_bytes(), max_bytes=65536))
            value = {'grant_id': grant['id'], 'deadline_at': grant['deadline_at'],
                     'status': LoopNativeResearchAuthority(store, registry).status(grant['id'])}
        else:
            value = revoke_loop_native_grant(store, registry, args.grant_id, args.reason)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
