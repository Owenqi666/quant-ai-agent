"""Real HTTP monthly fixture flow, immutable review/report, offline restore."""
from __future__ import annotations
import argparse
from pathlib import Path
import json
import socket
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_portable_release import clean_environment, stop_process
from paper_alpha.storage import atomic_json, digest
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.monthly_experiments import MonthlyExperiments
from paper_alpha.server.service import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    opener = build_opener(ProxyHandler({}))
    trace = []
    def request(path, body=None):
        req = Request(f'http://127.0.0.1:{port}' + path,
                      data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=30) as response:
            value = json.load(response)
            trace.append({'path': path, 'method': 'GET' if body is None else 'POST',
                          'status': response.status, 'response_digest': digest(value)})
        atomic_json(out / 'http-trace.json', trace)
        return value
    process = None
    try:
        with (out / 'server.log').open('wb') as stream:
            process = subprocess.Popen([sys.executable, '-m', 'paper_alpha.server.launcher',
                '--home', str(out / 'workspace'), '--port', str(port)], cwd=ROOT,
                env=clean_environment(), stdout=stream, stderr=stream, start_new_session=True)
        deadline = time.monotonic() + 45
        while True:
            try:
                health = request('/api/health')
                if health['worker']['online']:
                    break
            except OSError:
                pass
            assert process.poll() is None and time.monotonic() < deadline, 'Launcher not healthy'
            time.sleep(.1)
        rules = request('/api/research-protocols/presets')['presets'][1]['config']
        protocol = request('/api/research-protocols', {'title': 'Monthly MOM versus MOM-ID fixture',
            'note': 'Automation; explicit project convention; not human research approval.', 'config': rules})
        config = request('/api/monthly-experiments/defaults')['config']
        config['cost_bps'] = 5.0
        body = {'protocol_id': protocol['id'], 'protocol_digest': protocol['digest'],
                'config': config, 'idempotency_key': 'demo-monthly-create'}
        ack = request('/api/monthly-experiments', body)
        assert request('/api/monthly-experiments', body) == ack
        url = '/api/monthly-experiments/' + ack['experiment_id']
        deadline = time.monotonic() + 60
        while request(url + '/status')['status'] in {'queued', 'running', 'cancelling'}:
            assert time.monotonic() < deadline, 'Monthly worker deadline'
            time.sleep(.1)
        detail = request(url)
        assert detail['experiment']['status'] == 'completed', detail
        assert detail['verification']['reference_passed'] is True
        target = detail['review_target']
        review = request(url + '/reviews', {**target, 'verdict': 'needs_revision',
            'note': 'Software reference passed. Real source and economic assessment remain open.',
            'source': 'automation', 'idempotency_key': 'demo-monthly-review'})
        report_body = {**target, 'idempotency_key': 'demo-monthly-report'}
        report = request(url + '/reports', report_body)
        assert report['reviews'] == [review]
        request(url + '/reviews', {**target, 'verdict': 'needs_revision', 'note': 'Later record cannot alter earlier report.',
            'source': 'automation', 'idempotency_key': 'demo-monthly-later-review'})
        assert request(url + '/reports', report_body) == report
        assert request(url + '/reports/' + report['id'] + '/json') == report
        atomic_json(out / 'protocol.json', protocol)
        atomic_json(out / 'report.json', report)
        (out / 'report.md').write_text(report['markdown'], encoding='utf-8')
        final_detail = request(url)
        atomic_json(out / 'experiment.json', final_detail)
        stop_process(process)
        process = None
        create_backup(out / 'workspace', out / 'backup')
        restore_backup(out / 'backup', out / 'restored')
        service = MonthlyExperiments(Store(out / 'restored'))
        assert service.get(ack['experiment_id']) == final_detail
        assert service.get_report(ack['experiment_id'], report['id']) == report
        atomic_json(out / 'result.json', {'passed': True, 'schema': health['database_schema'],
            'experiment_id': ack['experiment_id'], 'reference_passed': True,
            'same_sample_comparison': detail['result']['summary'], 'frozen_report_digest': report['digest'],
            'backup_restored': True, 'scope': 'Controlled-fixture software validation; no market alpha, paper replication or human-efficiency claim.'})
        print({'passed': True, 'result': str(out / 'result.json')})
    finally:
        if process is not None:
            stop_process(process)


if __name__ == '__main__':
    main()
