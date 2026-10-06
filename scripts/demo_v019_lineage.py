"""New-dir synthetic row-reuse reservations, migration and refusal evidence.

This is an engineering control example, not a strategy experiment or a claim
that the reserved dates were never seen outside this local workbench.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from copy import deepcopy
import csv
import hashlib
import io
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.server import observation_identity as observations
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.db import SCHEMA_VERSION, connect, transaction
from paper_alpha.server.research_guard import ResearchGuard
from paper_alpha.server.service import Store, ServiceError
from paper_alpha.storage import atomic_json, digest, json_text, read_json

SCOPE = 'Engineering-policy checks on exact synthetic daily rows; no market source authentication, experiment returns, human labels, model calls or unseen-test claim.'


def _variant(store, name, mode):
    original = store._fetch('datasets', store.example_dataset_id)
    metadata = read_json(original['metadata_path'])
    with Path(original['data_path']).open(newline='') as stream:
        reader = csv.DictReader(stream)
        columns, rows = list(reader.fieldnames), list(reader)
    if mode == 'crop':
        metadata['calendar_dates'] = metadata['calendar_dates'][5:]
        rows = [row for row in rows if row['date'] in set(metadata['calendar_dates'])]
    if mode == 'subset':
        metadata['universe'] = metadata['universe'][:6]
        rows = [row for row in rows if row['asset'] in metadata['universe']]
    if mode == 'append':
        last = metadata['calendar_dates'][-1]
        rows.extend([{**row, 'date': '2023-07-17'} for row in rows if row['date'] == last])
        metadata['calendar_dates'].append('2023-07-17')
    if mode == 'encode':
        rows.sort(key=lambda row: (row['date'], tuple(-ord(char) for char in row['asset'])))
        metadata['universe'].reverse()
        for row in rows:
            row['volume'] = format(int(float(row['volume'])), '.8e')
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=columns, quoting=csv.QUOTE_ALL, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(rows)
    payload = stream.getvalue().encode()
    metadata.update(version='v019-' + name, market_sha256=hashlib.sha256(payload).hexdigest(),
                    rows=len(rows), sessions=len(metadata['calendar_dates']))
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'v019-lineage-demo:' + name))
    folder = store.root / 'datasets' / identity
    folder.mkdir()
    (folder / 'market.csv').write_bytes(payload)
    atomic_json(folder / 'metadata.json', metadata)
    with transaction(store.db_path) as connection:
        connection.execute('INSERT INTO datasets VALUES (?,?,?,?,?,?)',
            (identity, name, digest({'data': metadata['market_sha256'], 'metadata': metadata}), json_text(metadata),
             str(folder / 'market.csv'), str(folder / 'metadata.json')))
    return identity


def _counts(store):
    with closing(connect(store.db_path)) as connection:
        return {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('researches', 'revisions', 'runs', 'observation_identities',
                              'observation_rows', 'observation_revision_bindings', 'research_guard_boundaries')}


def build_demo(out):
    out = Path(out).resolve()
    if any(out.iterdir()):
        raise ValueError('Lineage demo requires a new empty directory')
    atomic_json(out / 'status.json', {'passed': False, 'status': 'running', 'scope': SCOPE})
    store = Store(out / 'workspace')
    paper = store.add_paper((ROOT / 'examples/alpha101/paper.pdf').read_bytes(), '101 Formulaic Alphas')
    raw = read_json(ROOT / 'examples/alpha101/task.json')
    task = {key: deepcopy(raw[key]) for key in ('evidence', 'hypotheses', 'candidates', 'evaluation', 'budget')}
    atomic_json(out / 'frozen-task.json', task)
    first = store.create_research('Original synthetic reservation', paper['id'], store.example_dataset_id, task, 'original')
    atomic_json(out / 'original-research.json', first)
    unsafe = deepcopy(task)
    unsafe['evaluation']['splits']['validation'] = {'start': '2023-04-03', 'end': '2023-06-30'}
    unsafe['evaluation']['splits']['test'] = {'start': '2023-07-03', 'end': '2023-07-14'}
    checks, datasets = {}, {}
    for mode in ('encode', 'crop', 'append', 'subset'):
        dataset = _variant(store, mode, mode)
        datasets[mode] = dataset
        proposed = deepcopy(unsafe)
        if mode == 'crop':
            proposed['evaluation']['splits']['train']['start'] = '2022-01-10'
        atomic_json(out / f'{mode}-request.json', {'dataset_id': dataset, 'task': proposed})
        before = _counts(store)
        preflight = ResearchGuard(store).check(dataset, proposed)
        atomic_json(out / f'{mode}-preflight.json', preflight)
        try:
            store.create_research('Attempt to reuse reserved rows', paper['id'], dataset, proposed, 'unsafe-' + mode)
        except ServiceError as exc:
            response = {'rejected': True, 'status': exc.status, 'reason': str(exc),
                        'counts_before': before, 'counts_after': _counts(store)}
            checks[mode + '_reserved_reuse_refused'] = (not preflight['allowed'] and exc.status == 409
                                                       and 'reserved test interval' in str(exc)
                                                       and before == response['counts_after'])
            atomic_json(out / f'{mode}-refusal.json', response)
        else:
            raise AssertionError('Reserved row reuse was accepted')
    copied = store.create_research('Same splits, accepted encoding', paper['id'], datasets['encode'], task, 'safe-encoded')
    atomic_json(out / 'safe-reencoded-research.json', copied)
    status = ResearchGuard(store).get(copied['id'])
    atomic_json(out / 'shared-reservation-status.json', status)
    with closing(connect(store.db_path)) as connection:
        identities = [dict(row) for row in connection.execute('SELECT * FROM observation_identities ORDER BY dataset_id')]
        samples = [dict(row) for row in connection.execute('SELECT * FROM observation_rows ORDER BY dataset_id,date,asset LIMIT 8')]
    atomic_json(out / 'index-summary.json', {'version': observations.CANONICAL_VERSION, 'identities': identities,
                                           'row_samples': samples, 'full_index_in': 'workspace/workbench.sqlite3'})
    old = next(item for item in identities if item['dataset_id'] == store.example_dataset_id)
    new = next(item for item in identities if item['dataset_id'] == datasets['encode'])
    checks['engine_equivalent_panel_identity'] = old['panel_sha256'] == new['panel_sha256'] and old['market_sha256'] != new['market_sha256']
    checks['related_boundaries_visible'] = status['status'] == 'protected' and status['total_boundaries'] == 2

    # A separate new backup clone retains the original example; destructive
    # negative controls and schema downgrade never touch a user's workspace.
    create_backup(store.root, out / 'index-backup')
    restore_backup(out / 'index-backup', out / 'damaged-clone')
    damaged = Store(out / 'damaged-clone')
    with transaction(damaged.db_path) as connection:
        connection.execute('DELETE FROM observation_rows WHERE dataset_id=? AND date=?', (store.example_dataset_id, '2022-01-03'))
    damaged_check = ResearchGuard(damaged).check(datasets['encode'], task)
    atomic_json(out / 'damaged-index-refusal.json', damaged_check)
    checks['damaged_historical_index_refused'] = not damaged_check['allowed'] and 'missing or changed' in damaged_check['reason']

    from paper_alpha.server.domain_research_jobs import SCHEMA as DOMAIN_SCHEMA
    from paper_alpha.server.semantic_annotations import SCHEMA as SEMANTIC_SCHEMA
    from paper_alpha.server.claim_reviews import SCHEMA as CLAIM_REVIEW_SCHEMA
    from paper_alpha.server.semantic_evaluation_sets import SCHEMA as SEMANTIC_SET_SCHEMA
    from paper_alpha.server.research_bindings import SCHEMA as BINDING_SCHEMA
    from paper_alpha.server.industry_mom_storage_schema import SCHEMA as INDUSTRY_MOM_SCHEMA
    restore_backup(out / 'index-backup', out / 'migration-clone')
    clone = Store(out / 'migration-clone')
    # Reconstruct the actual schema14 boundary in this disposable clone. All
    # schemas installed AFTER 14 must be removed in reverse dependency order;
    # merely lowering its version marker leaves later tables behind and would
    # correctly fail the strict production CREATE TABLE migration.
    schema15 = observations.SCHEMA + DOMAIN_SCHEMA + SEMANTIC_SCHEMA
    schema16 = CLAIM_REVIEW_SCHEMA + SEMANTIC_SET_SCHEMA + BINDING_SCHEMA
    tables = re.findall(r'CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)', schema15 + schema16 + INDUSTRY_MOM_SCHEMA)
    schema16_tables = re.findall(r'CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)', schema16)
    schema17_tables = re.findall(r'CREATE TABLE\s+(\w+)', INDUSTRY_MOM_SCHEMA)
    if len(tables) != len(set(tables)):
        raise AssertionError('Later schema declarations contain duplicate table identities')
    with transaction(clone.db_path) as connection:
        current_tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        original_version = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]
        if original_version != str(SCHEMA_VERSION) or not set(tables) <= current_tables:
            raise AssertionError('Isolated current-schema clone does not contain every declared later table')
        if any(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in schema16_tables):
            raise AssertionError('The v019 isolated fixture must not contain v020 business records')
        for table in reversed(tables):
            connection.execute(f'DROP TABLE {table}')
        connection.execute("UPDATE settings SET value='14' WHERE key='schema_version'")
        connection.execute('DELETE FROM schema_migrations WHERE version>=15')
        fixture_tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        expected_fixture_tables = current_tables - set(tables)
        if fixture_tables != expected_fixture_tables or set(tables) & fixture_tables:
            raise AssertionError('Isolated schema14 fixture retains a later table or removed an original table')
        business = {name: [dict(row) for row in connection.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                    for name in sorted(fixture_tables - {'settings', 'schema_migrations'})}
        settings = [dict(row) for row in connection.execute("SELECT * FROM settings WHERE key!='schema_version' ORDER BY key")]
        old_migrations = [dict(row) for row in connection.execute('SELECT * FROM schema_migrations ORDER BY version')]
    atomic_json(out / 'schema14-fixture.json', {'schema_version': 14, 'original_schema_version': original_version,
        'expected_current_schema_version': SCHEMA_VERSION, 'removed_later_tables': tables,
        'schema16_tables': schema16_tables, 'remaining_tables': sorted(fixture_tables),
        'original_business_digests': {name: digest(rows) for name, rows in business.items()},
        'original_settings_digest': digest(settings), 'original_migrations': old_migrations})
    migrated = Store(clone.root)
    with closing(connect(migrated.db_path)) as connection:
        after = {name: [dict(row) for row in connection.execute(f'SELECT * FROM {name} ORDER BY rowid')] for name in business}
        version = connection.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]
        restored_tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        later_table_counts = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                              for table in schema16_tables if table in restored_tables}
        industry_table_counts = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                                 for table in schema17_tables if table in restored_tables}
        after_settings = [dict(row) for row in connection.execute("SELECT * FROM settings WHERE key!='schema_version' ORDER BY key")]
        after_migrations = [dict(row) for row in connection.execute('SELECT * FROM schema_migrations ORDER BY version')]
    migration_checks = {'current_schema_restored': version == str(SCHEMA_VERSION),
        'complete_original_table_set_restored': restored_tables == current_tables,
        'all_original_business_preserved': business == after,
        'original_settings_preserved': settings == after_settings,
        'original_migration_history_preserved': old_migrations == [row for row in after_migrations if row['version'] <= 14],
        'later_migration_versions_recorded': [row['version'] for row in after_migrations if row['version'] >= 15] == list(range(15, SCHEMA_VERSION + 1)),
        'schema16_resources_created_empty': set(later_table_counts) == set(schema16_tables) and not any(later_table_counts.values()),
        'schema17_resources_created_empty': set(industry_table_counts) == set(schema17_tables) and not any(industry_table_counts.values())}
    atomic_json(out / 'migration-comparison.json', {'passed': all(migration_checks.values()), 'checks': migration_checks,
        'schema_version': version, 'expected_schema_version': SCHEMA_VERSION,
        'schema16_table_counts': later_table_counts, 'restored_tables': sorted(restored_tables),
        'original_business_digests': {name: digest(rows) for name, rows in business.items()},
        'restored_business_digests': {name: digest(rows) for name, rows in after.items()},
        'migrations': after_migrations})
    checks['schema14_migration_preserves_original_authority'] = all(migration_checks.values())
    migration_check = ResearchGuard(migrated).check(datasets['encode'], unsafe)
    atomic_json(out / 'migration-refusal.json', migration_check)
    checks['restored_migration_keeps_reservations'] = not migration_check['allowed'] and 'reserved test interval' in migration_check['reason']
    result = {'passed': all(checks.values()), 'scope': SCOPE, 'canonical_version': observations.CANONICAL_VERSION,
              'checks': checks, 'checks_passed': sum(checks.values()), 'checks_total': len(checks),
              'llm_api_called': False, 'human_reviews_written': 0, 'runs_submitted': 0,
              'workspace': str(store.root), 'limitations': status['limitations']}
    atomic_json(out / 'result.json', result)
    atomic_json(out / 'status.json', {'passed': result['passed'], 'status': 'completed', 'scope': SCOPE})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    try:
        result = build_demo(out)
    except Exception as exc:
        atomic_json(out / 'result.json', {'passed': False, 'scope': SCOPE, 'error': type(exc).__name__ + ': ' + str(exc)})
        raise
    print(json_text({'passed': result['passed'], 'checks_passed': result['checks_passed'], 'checks_total': result['checks_total'], 'out': str(out)}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
