#!/usr/bin/env python3
"""Explicit local grant administration. Never launches a model or a service.

Create: .venv/bin/python -B scripts/manage_research_host_grant.py create \
  --workspace /absolute/new/workspace --provider-config /absolute/providers.json \
  --allow-real --spec /absolute/frozen-grant-spec.json
Read: use `inspect` with the same host configuration.
Revoke: `revoke ... --grant-id host_grant_... --reason 'Stopped by owner'`.

Spec fields are HostGrantCreate; paper_selections entries have kind=paper_selection,
paper_id and selected_pages. generation_policy supplies dataset_id, evaluation,
method_policy, destination={kind:new_research}, title. Default bounds are two
attempts (one per operation), 240 seconds each, 1800 seconds across sessions.
For the three-call qualification batch explicitly set max_dispatches=3,
max_extract=2, max_generate=1 and list both frozen extraction selections.
Creating a bounded_qualification grant is explicit permission for that limited
attempt inventory; it is not prior scientific/native qualification. `qualified`
requires independently reverified host-native accepted proposal IDs per operation.
"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper_alpha.server.provider_runtime import load_provider_registry
from paper_alpha.server.provider_host_authority import create_host_grant,revoke_host_grant
from paper_alpha.server.service import Store
from paper_alpha.server.provider_contract import strict_json


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['create','inspect','revoke'])
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--provider-config',type=Path,required=True)
    parser.add_argument('--allow-real',action='store_true')
    parser.add_argument('--allow-fixture',action='store_true')
    parser.add_argument('--spec',type=Path)
    parser.add_argument('--grant-id')
    parser.add_argument('--reason')
    args=parser.parse_args(argv)
    if not args.workspace.is_absolute() or not (args.workspace/'workbench.sqlite3').is_file():
        parser.error('Use an existing initialized absolute workspace; this tool never creates a placeholder workspace')
    registry=load_provider_registry(args.provider_config,allow_real=args.allow_real,allow_fixture=args.allow_fixture)
    if args.action=='inspect':
        # Store construction runs migrations; inspection explicitly avoids it.
        from types import SimpleNamespace
        store=SimpleNamespace(root=args.workspace,db_path=args.workspace/'workbench.sqlite3')
        value=registry.research_authority(store).capabilities()
    else:
        store=Store(args.workspace)
        if args.action=='create':
            if args.spec is None: parser.error('create requires --spec')
            value=create_host_grant(store,registry,**strict_json(args.spec.read_bytes(),max_bytes=65536))
            value={'host_grant_id':value['id'],'deadline_at':value['deadline_at'],'capabilities':registry.research_authority(store).capabilities()}
        else:
            if not args.grant_id or not args.reason: parser.error('revoke requires --grant-id and --reason')
            value=revoke_host_grant(store,registry,args.grant_id,args.reason)
    print(json.dumps(value,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
