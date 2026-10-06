"""Verify schema15→current additive migration, including v0.20 resources."""
import argparse
import json
from pathlib import Path

from check_v018_upgrade import snapshot
from paper_alpha.server.backup import restore_backup
from paper_alpha.server.service import Store
from paper_alpha.server.db import SCHEMA_VERSION
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_tools import ResearchTools
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.server.semantic_annotations import SemanticAnnotations
from paper_alpha.storage import digest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = {'passed': False, 'scope': 'New-directory clone only; original workspace untouched.'}
    try:
        home = out / 'workspace'
        restore_backup(args.backup.resolve(), home)
        before, files = snapshot(home)
        if dict(before['settings']).get('schema_version') != '15':
            raise ValueError('Expected frozen schema15 backup')
        store = Store(home)
        after, after_files = snapshot(home)
        preserved = {table: after[table] == rows for table, rows in before.items()
            if table not in {'settings', 'schema_migrations'}}
        old_settings, new_settings = dict(before['settings']), dict(after['settings'])
        old_settings.pop('schema_version'); new_settings.pop('schema_version')
        cases = {row['id']: ResearchCases(store).get(row['id'])['digest'] for row in store._read('SELECT id FROM research_cases')}
        tools = {row['id']: digest(ResearchTools(store).get(row['id'])) for row in store._read('SELECT id FROM research_tool_sessions')}
        claims = {row['id']: ResearchClaims(store).get(row['id'])['digest'] for row in store._read('SELECT id FROM research_claims')}
        summary = SemanticAnnotations(store).summary()
        new_tables = sorted(set(after) - set(before))
        result.update(schema=dict(after['settings'])['schema_version'], business_tables_preserved=preserved,
            settings_preserved=old_settings == new_settings,
            migration_history_preserved=all(row in after['schema_migrations'] for row in before['schema_migrations']),
            historical_files=len(files), historical_files_preserved=files == after_files,
            case_digests=cases, tool_digests=tools, claim_digests=claims,
            new_tables=new_tables, new_tables_empty=all(not after[name] for name in new_tables),
            semantic_human_records=summary['human_records'], semantic_pending_cases=summary['pending_cases'])
        result['passed'] = (all(preserved.values()) and result['settings_preserved']
            and result['migration_history_preserved'] and result['historical_files_preserved']
            and result['schema'] == str(SCHEMA_VERSION) and result['new_tables_empty'])
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'out': str(out)}, ensure_ascii=False))
    return 0 if result['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
