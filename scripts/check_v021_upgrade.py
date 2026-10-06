"""Verify a schema16 backup's additive industry-MOM migration in a fresh clone."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from check_v018_upgrade import snapshot
from paper_alpha.server.backup import restore_backup
from paper_alpha.server.db import SCHEMA_VERSION
from paper_alpha.server.service import Store
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.server.claim_reviews import ClaimReviews
from paper_alpha.storage import atomic_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = {'passed': False, 'scope': 'Fresh-directory schema16 clone; original workspace untouched.'}
    try:
        home = out / 'workspace'
        restore_backup(args.backup.absolute(), home)
        before, files = snapshot(home)
        if dict(before['settings']).get('schema_version') != '16':
            raise ValueError('Expected a frozen schema16 backup')
        store = Store(home)
        after, after_files = snapshot(home)
        preserved = {table: after[table] == rows for table, rows in before.items()
                     if table not in {'settings', 'schema_migrations'}}
        previous_settings, current_settings = dict(before['settings']), dict(after['settings'])
        previous_settings.pop('schema_version'); current_settings.pop('schema_version')
        new_tables = sorted(set(after) - set(before))
        cases = {row['id']: ResearchCases(store).get(row['id'])['digest']
                 for row in store._read('SELECT id FROM research_cases')}
        claims = {row['id']: ResearchClaims(store).get(row['id'])['digest']
                  for row in store._read('SELECT id FROM research_claims')}
        reviews = {row['id']: ClaimReviews(store).get(row['id'])['digest']
                   for row in store._read('SELECT id FROM claim_reviews')}
        result.update(database_schema=dict(after['settings'])['schema_version'],
            business_tables_preserved=preserved, settings_preserved=previous_settings == current_settings,
            migration_history_preserved=all(row in after['schema_migrations'] for row in before['schema_migrations']),
            historical_files=len(files), historical_files_preserved=files == after_files,
            historical_case_digests=cases, historical_claim_digests=claims, historical_review_digests=reviews,
            new_tables=new_tables, new_tables_empty=all(not after[name] for name in new_tables))
        expected = {'industry_mom_sources', 'industry_mom_experiments', 'industry_mom_attempts',
                    'industry_mom_events', 'industry_mom_receipts'}
        result['passed'] = (all(preserved.values()) and result['settings_preserved']
            and result['migration_history_preserved'] and result['historical_files_preserved']
            and result['database_schema'] == str(SCHEMA_VERSION)
            and set(new_tables) == expected and result['new_tables_empty'])
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    atomic_json(out / 'result.json', result)
    print(json.dumps({'passed': result['passed'], 'out': str(out), 'error': result.get('error')}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
