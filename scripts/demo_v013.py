"""Reproducible rule variants, fixture diagnostics, and persistence/restore evidence."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.research_protocol import presets
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.research_protocols import ResearchProtocols
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, read_json
from preview_research_protocol import freeze, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    service = ResearchProtocols(Store(out / 'workspace'))
    records = []
    paper, project = (item['config'] for item in presets())
    variants = {'paper': paper, 'project_complete': project,
                'project_available': {**project, 'missing_policy': 'available', 'min_coverage': 0.9, 'max_missing_run': 1},
                'project_six_months': {**project, 'mom_window_months': 6, 'id_window_months': 6}}
    summaries = {}
    for label, config in variants.items():
        request = {'title': label, 'note': 'Automated controlled fixture; no human judgment or market-performance claim.',
                   'config': config, 'parent_id': None if not records else records[0 if label == 'project_complete' else 1]['id']}
        record = service.create(**request)
        assert service.create(**request) == record
        records.append(record)
        summaries[label] = freeze(out / label, config, '2026-07')
        assert summaries[label]['config_digest'] == record['config_digest']
        assert verify(out / label)['verified']
    reports = {label: read_json(out / label / 'report.json') for label in variants}
    assert reports['paper']['status'] == 'blocked'
    complete = {row['asset']: row for row in reports['project_complete']['assets']}
    available = {row['asset']: row for row in reports['project_available']['assets']}
    assert complete['MISSING_ROW']['momentum'] is None
    assert available['MISSING_ROW']['momentum'] is not None
    assert available['SHORT_HISTORY']['momentum'] is None
    assert reports['project_six_months']['windows']['momentum']['start'] == '2025-12-01'
    create_backup(out / 'workspace', out / 'backup')
    restore_backup(out / 'backup', out / 'restored')
    restored = ResearchProtocols(Store(out / 'restored'))
    assert all(restored.get(record['id']) == record for record in records)
    atomic_json(out / 'saved-records.json', records)
    atomic_json(out / 'result.json', {'passed': True, 'diagnostics': summaries, 'records_restored': len(records),
                'scope': 'Controlled-fixture software verification. No real-market returns or human review results.'})
    print({'passed': True, 'result': str(out / 'result.json')})


if __name__ == '__main__':
    main()
