"""Isolated author-panel HTTP import/replay/read/report and backup/restore flow.

The default input is synthetic. --input accepts an existing normalized slice;
HTTP verification never claims to re-read or authenticate the raw MAT archive.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_portable_release import clean_environment, stop_process
from paper_alpha.author_archive_contract import ADAPTER_VERSION, source_metadata
from paper_alpha.author_panel import validate_panel
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.author_panels import AuthorPanels
from paper_alpha.server.service import Store
from paper_alpha.storage import atomic_json, digest, read_json


def synthetic_panel():
    return {'schema_version': 1, 'kind': 'author_perturbed_monthly_panel', 'adapter_version': ADAPTER_VERSION,
        'source': source_metadata('USData.mat'), 'selection': {'target_month': '2020-01', 'row_offset': 0, 'row_count': 1},
        'months': [f'2019-{i:02d}' for i in range(1, 13)] + ['2020-01'],
        'source_observation_dates': [f'2019-{i:02d}-28' for i in range(1, 13)] + ['2020-01-28'],
        'rows': [{'source_row': 1, 'asset': 'USData.mat:row:1', 'country': None,
                  'returns': [.1] * 11 + [9., None], 'return_states': ['value'] * 12 + ['nan'],
                  'dgw': .2, 'dgw_state': 'value', 'market_cap': 100., 'market_cap_state': 'value'}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=False)
    panel = validate_panel(read_json(args.input) if args.input else synthetic_panel())
    atomic_json(out / 'input.json', panel)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    opener = build_opener(ProxyHandler({}))
    trace = []
    def request(path, body=None, markdown=False):
        req = Request(f'http://127.0.0.1:{port}' + path,
                      data=None if body is None else json.dumps(body, allow_nan=False).encode(),
                      headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=30) as response:
            raw = response.read().decode('utf-8')
            value = raw if markdown else json.loads(raw)
            trace.append({'path': path, 'method': 'GET' if body is None else 'POST',
                          'status': response.status, 'response_digest': digest(value)})
        atomic_json(out / 'http-trace.json', trace)
        return value
    process = None
    try:
        with (out / 'server.log').open('wb') as stream:
            process = subprocess.Popen([sys.executable, '-m', 'paper_alpha.server.launcher', '--home', str(out / 'workspace'), '--port', str(port)],
                cwd=ROOT, env=clean_environment(), stdout=stream, stderr=stream, start_new_session=True)
        deadline = time.monotonic() + 45
        while True:
            try:
                health = request('/api/health')
                if health['worker']['online']: break
            except OSError: pass
            assert process.poll() is None and time.monotonic() < deadline, 'Launcher not healthy'
            time.sleep(.1)
        body = {'title': 'Author source alignment import' if args.input else 'Synthetic author-panel API contract demonstration',
                'note': 'Automated source import, not human approval. Server verifies normalized panel only.',
                'panel': panel, 'idempotency_key': 'v015-demo-author-import'}
        detail = request('/api/author-panels', body)
        assert request('/api/author-panels', body) == detail, 'Replay changed the frozen record'
        route = '/api/author-panels/' + detail['id']
        assert request(route) == detail
        listing = request('/api/author-panels?limit=1&offset=0')
        assert listing['total'] == 1 and listing['items'][0]['id'] == detail['id']
        assert detail['reference']['passed'] is True and detail['raw_source_reverified'] is False
        assert detail['result']['panel_digest'] == digest(panel)
        if not args.input:
            row = detail['result']['rows'][0]
            assert row['formation_ready'] is True and row['label_ready'] is False
            assert abs(row['momentum'] - (1.1 ** 11 - 1)) < 1e-12
        markdown = request(route + '/markdown', markdown=True)
        atomic_json(out / 'detail.json', detail)
        (out / 'report.md').write_text(markdown, encoding='utf-8')
        stop_process(process); process = None
        create_backup(out / 'workspace', out / 'backup')
        restore_backup(out / 'backup', out / 'restored')
        restored = AuthorPanels(Store(out / 'restored'))
        assert restored.get(detail['id']) == detail
        assert restored.create(**body) == detail
        assert restored.markdown(detail['id']) == markdown
        result = {'passed': True, 'schema': health['database_schema'], 'panel_id': detail['id'],
                  'panel_digest': detail['result']['panel_digest'], 'summary': detail['result']['summary'],
                  'reference_passed': True, 'backup_restored': True, 'idempotency_replayed_after_restore': True,
                  'raw_source_reverified': False, 'input_origin': str(args.input.resolve()) if args.input else 'synthetic-contract-fixture',
                  'scope': 'Normalized-panel software acceptance. No raw-source authentication, market alpha or human-efficiency claim.'}
        atomic_json(out / 'result.json', result)
        print(json.dumps({'passed': True, 'result': str(out / 'result.json')}))
    finally:
        if process is not None: stop_process(process)


if __name__ == '__main__':
    main()
