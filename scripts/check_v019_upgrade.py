"""Verify additive schema14→15 migration on a new restored directory only."""
import argparse
import json
from pathlib import Path

from check_v018_upgrade import snapshot
from paper_alpha.server.backup import restore_backup
from paper_alpha.server.service import Store
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_tools import ResearchTools
from paper_alpha.server.research_claims import ResearchClaims
from paper_alpha.server.semantic_annotations import SemanticAnnotations
from paper_alpha.storage import digest

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--backup',required=True,type=Path);p.add_argument('--out',required=True,type=Path);args=p.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False);result={'passed':False,'scope':'New-directory clone only; original workspace untouched.'}
    try:
        home=out/'workspace';restore_backup(args.backup.resolve(),home);before,files=snapshot(home)
        if dict(before['settings']).get('schema_version')!='14':raise ValueError('Expected frozen schema14 backup')
        store=Store(home);after,after_files=snapshot(home)
        preserved={table:after[table]==rows for table,rows in before.items() if table not in {'settings','schema_migrations'}}
        a=dict(before['settings']);b=dict(after['settings']);a.pop('schema_version');b.pop('schema_version')
        cases={row['id']:ResearchCases(store).get(row['id'])['digest'] for row in store._read('SELECT id FROM research_cases')}
        tools={row['id']:digest(ResearchTools(store).get(row['id'])) for row in store._read('SELECT id FROM research_tool_sessions')}
        claims={row['id']:ResearchClaims(store).get(row['id'])['digest'] for row in store._read('SELECT id FROM research_claims')}
        summary=SemanticAnnotations(store).summary()
        result.update(schema=dict(after['settings'])['schema_version'],business_tables_preserved=preserved,settings_preserved=a==b,
            migration_history_preserved=all(row in after['schema_migrations'] for row in before['schema_migrations']),
            historical_files=len(files),historical_files_preserved=files==after_files,case_digests=cases,tool_digests=tools,claim_digests=claims,
            new_tables=sorted(set(after)-set(before)),observation_identities=len(store._read('SELECT dataset_id FROM observation_identities')),
            unresolved_observation_identities=len(store._read('SELECT dataset_id FROM observation_unresolved')),
            semantic_human_records=summary['human_records'],semantic_pending_cases=summary['pending_cases'],domain_jobs=len(store._read('SELECT id FROM domain_research_jobs')))
        result['passed']=all(preserved.values()) and a==b and result['migration_history_preserved'] and files==after_files and result['schema']=='15' and summary['human_records']==0 and result['domain_jobs']==0
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'passed':result['passed'],'out':str(out)},ensure_ascii=False));return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
