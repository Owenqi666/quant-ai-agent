"""Verify additive migration of a frozen schema13 backup to the current schema."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from paper_alpha.server.backup import restore_backup, EXCLUDED
from paper_alpha.server.service import Store
from paper_alpha.server.db import SCHEMA_VERSION
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_tools import ResearchTools
from paper_alpha.storage import digest


def snapshot(home):
    with sqlite3.connect(home / 'workbench.sqlite3') as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        rows = {table: sorted([list(row) for row in connection.execute(f'SELECT * FROM "{table}"')],key=lambda row:json.dumps(row,sort_keys=True)) for table in tables}
    files = {str(path.relative_to(home)):hashlib.sha256(path.read_bytes()).hexdigest()
             for path in home.rglob('*') if path.is_file() and path.name not in EXCLUDED
             and path.name!='workbench.sqlite3'}
    return rows,files


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup',required=True,type=Path);parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'scope':'Clone upgrade only; original workspace untouched.'}
    try:
        home=out/'workspace';restore_backup(args.backup.resolve(),home)
        before,files=snapshot(home)
        if dict(before['settings']).get('schema_version')!='13':raise ValueError('Expected frozen schema13 backup')
        store=Store(home);after,after_files=snapshot(home)
        preserved={table:after[table]==rows for table,rows in before.items() if table not in {'settings','schema_migrations'}}
        settings_before=dict(before['settings']);settings_after=dict(after['settings'])
        settings_before.pop('schema_version');settings_after.pop('schema_version')
        old_cases={row['id']:ResearchCases(store).get(row['id']) for row in store._read('SELECT id FROM research_cases')}
        sessions={row['id']:ResearchTools(store).get(row['id']) for row in store._read('SELECT id FROM research_tool_sessions')}
        result.update(schema=store._read("SELECT value FROM settings WHERE key='schema_version'")[0]['value'],
            business_tables_preserved=preserved,settings_preserved=settings_before==settings_after,
            migration_history_preserved=all(row in after['schema_migrations'] for row in before['schema_migrations']),
            historical_files=len(files),historical_files_preserved=files==after_files,
            historical_case_digests={key:value['digest'] for key,value in old_cases.items()},
            historical_sessions={key:{'calls':value['usage']['calls'],'digest':digest(value)} for key,value in sessions.items()},
            new_tables=sorted(set(after)-set(before)),
            guard_boundaries=len(store._read('SELECT revision_id FROM research_guard_boundaries')))
        result['passed']=(all(preserved.values()) and result['settings_preserved'] and result['migration_history_preserved']
            and result['historical_files_preserved'] and result['schema']==str(SCHEMA_VERSION))
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
