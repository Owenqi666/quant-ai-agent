"""Validate an allowlisted bundle in a new directory, venv and real HTTP process.

The application runs only from the extracted source. Optional Playwright is an
external test driver, never an application runtime dependency.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import venv

from build_source_release import ROOT, create_bundle, extract_bundle, json_bytes, sha256


def clean_environment():
    env = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PAPER_ALPHA_HOME', 'PAPER_ALPHA_WORKER_LOCK_FD'):
        env.pop(name, None)
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    return env


def stop_process(process, timeout=15):
    """Ask the launcher to drain both children, then kill its owned group if stuck."""
    if process is None:
        return None
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        code = process.wait(timeout=5)
    # Also remove a child surviving an unexpectedly dead launcher.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    return code


def run_check(out: Path, archive: Path | None = None, browser_module: Path | None = None,
              browser_channel='chromium', timeout=120) -> dict:
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = {'schema_version': 1, 'passed': False, 'created_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Local/personal source relocation and synthetic HTTP evaluation; no public distribution, AI or market-performance claim.',
              'python': platform.python_version(), 'platform': platform.platform(),
              'original_checkout': str(ROOT), 'steps': [],
              'browser': {'status': 'not_run' if browser_module is not None else 'not_requested'}}
    process = None
    port = None
    log_stream = None
    env = clean_environment()
    trace = []
    try:
        if archive is None:
            built = create_bundle(ROOT, out / 'package')
            archive = Path(built['archive'])
        archive = archive.resolve()
        result['archive'] = str(archive)
        result['archive_sha256'] = sha256(archive.read_bytes())
        source, manifest = extract_bundle(archive, out / 'unpacked')
        result.update(source=str(source), version=manifest['version'], content_sha256=manifest['content_sha256'],
                      source_file_count=len(manifest['files']))
        cwd = out / 'unrelated-working-directory'
        cwd.mkdir()
        result['launch_cwd'] = str(cwd)
        venv.EnvBuilder(with_pip=True).create(out / 'venv')
        python = out / 'venv' / 'bin' / 'python'
        result['python_executable'] = str(python)

        def command(name, argv, limit=600):
            started = time.monotonic()
            log = out / (name + '.log')
            record = {'name': name, 'argv': list(map(str, argv)), 'cwd': str(cwd), 'log': log.name}
            result['steps'].append(record)
            try:
                with log.open('wb') as stream:
                    completed = subprocess.run(list(map(str, argv)), cwd=cwd, env=env, stdout=stream,
                                               stderr=subprocess.STDOUT, timeout=limit)
                record['exit_code'] = completed.returncode
                if completed.returncode:
                    raise RuntimeError(f'{name} failed; inspect {log}')
                return log.read_text()
            except subprocess.TimeoutExpired:
                record['timed_out'] = True
                raise
            finally:
                record['elapsed_seconds'] = time.monotonic() - started
                record['log_sha256'] = sha256(log.read_bytes())

        command('install-dependencies', [python, '-m', 'pip', 'install', '-r', source / 'requirements-web-lock.txt'])
        command('install-source', [python, '-m', 'pip', 'install', '--no-deps', '-e', source])
        # Probe several critical modules, with the same interpreter/cwd/env as launcher.
        code = '''
import importlib, importlib.metadata, json, pathlib, sys
names = ['paper_alpha', 'paper_alpha.evaluation', 'paper_alpha.server.api', 'paper_alpha.server.runner', 'paper_alpha.server.launcher']
print(json.dumps({'version': importlib.metadata.version('paper-to-alpha'), 'executable':sys.executable,
                 'modules': {name: str(pathlib.Path(importlib.import_module(name).__file__).resolve()) for name in names}}))
'''
        identity = json.loads(command('module-origins', [python, '-c', code]))
        if identity['version'] != manifest['version'] or any(not Path(path).is_relative_to(source) for path in identity['modules'].values()):
            raise RuntimeError('Module origin/version differs from the extracted source package')
        if any(Path(path).is_relative_to(ROOT / 'paper_alpha') for path in identity['modules'].values()):
            raise RuntimeError('Original checkout contaminated module resolution')
        result['module_identity'] = identity
        command('installed-packages', [python, '-m', 'pip', 'freeze', '--all'])
        command('dependency-consistency', [python, '-m', 'pip', 'check'])
        command('relocated-workflow-receipts', [python, '-m', 'unittest', 'discover',
                '-s', source / 'tests', '-t', source, '-p', 'test_workflow_receipts_v012.py', '-v'])
        command('relocated-research-protocols', [python, source / 'scripts/demo_v013.py',
                '--out', out / 'research-protocol-flow'])
        command('relocated-author-panels', [python, source / 'scripts/demo_v015.py',
                '--out', out / 'author-panel-flow'])
        command('relocated-author-numerics', [python, '-m', 'unittest', 'discover',
                '-s', source / 'tests', '-t', source, '-p', 'test_author_*.py', '-v'])
        command('relocated-author-studies', [python, source / 'scripts/demo_v016.py',
                '--out', out / 'author-study-flow'])
        command('relocated-research-tasks', [python, source / 'scripts/demo_v017.py',
                '--out', out / 'research-task-flow'])
        command('relocated-research-decisions', [python, source / 'scripts/evaluate_v017.py',
                '--out', out / 'research-decision-evaluation'])
        command('relocated-research-execution', [python, source / 'scripts/demo_v018.py', '--out', out / 'research-execution-flow'])
        command('relocated-referenced-research', [python, source / 'scripts/evaluate_v018.py', '--out', out / 'referenced-research-evaluation'])
        command('relocated-cross-domain-lineage-semantics', [python, source / 'scripts/demo_v019.py', '--out', out / 'v019-flow'])
        command('relocated-exact-review-reference-binding', [python, source / 'scripts/demo_v020.py', '--out', out / 'v020-flow'])
        command('relocated-research-acceptance-samples', [python, '-B',
                source / 'scripts/demo_v022_research_acceptance.py', '--out', out / 'research-acceptance-samples'])
        command('relocated-eligibility-numerics', [python, '-m', 'unittest', 'discover',
                '-s', source / 'tests', '-t', source, '-p', 'test_eligibility*.py', '-v'])
        command('relocated-monthly-experiments', [python, source / 'scripts/demo_v014.py',
                '--out', out / 'monthly-experiment-flow'])
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        result['base_url'] = base
        opener = build_opener(ProxyHandler({}))

        def request(path, body=None, parse_json=True):
            started = time.monotonic()
            data = None if body is None else json_bytes(body)
            req = Request(base + path, data=data, headers={'Content-Type': 'application/json'})
            with opener.open(req, timeout=30) as response:
                payload = response.read()
                record = {'method': 'GET' if body is None else 'POST', 'path': path,
                          'status': response.status, 'bytes': len(payload), 'sha256': sha256(payload),
                          'elapsed_seconds': time.monotonic() - started}
            value = json.loads(payload) if parse_json else payload
            if parse_json:
                record['response'] = value
            trace.append(record)
            (out / 'http-trace.json').write_bytes(json_bytes(trace))
            return value

        launcher_argv = [str(python), '-m', 'paper_alpha.server.launcher', '--home', str(out / 'workspace'), '--port', str(port)]
        result['launcher_argv'] = launcher_argv
        log_stream = (out / 'server.log').open('wb')
        process = subprocess.Popen(launcher_argv, cwd=cwd, env=env, stdout=log_stream, stderr=log_stream, start_new_session=True)
        result['launcher_pid'] = process.pid
        deadline = time.monotonic() + min(timeout, 45)
        while True:
            try:
                health = request('/api/health')
                if health.get('status') == 'ok' and health.get('worker', {}).get('online'):
                    break
            except OSError:
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('Extracted launcher/API/worker did not become healthy; inspect server.log')
            time.sleep(.25)
        if health['ai_enabled'] is not False or health['version'] != manifest['version']:
            raise RuntimeError('Running service identity does not match the package')
        result['health'] = health
        protocol_config = request('/api/research-protocols/presets')['presets'][1]['config']
        protocol_preview = request('/api/research-protocols/preview', {'config': protocol_config, 'target_month': '2026-07'})
        protocol_request = {'title': 'Portable protocol fixture', 'note': 'Automated software check', 'config': protocol_config}
        protocol_record = request('/api/research-protocols', protocol_request)
        if protocol_record['config_digest'] != protocol_preview['config_digest'] or request('/api/research-protocols', protocol_request) != protocol_record:
            raise RuntimeError('Relocated protocol preview, snapshot or replay differs')
        result['research_protocols'] = {'verified': True, 'record_id': protocol_record['id'], 'config_digest': protocol_record['config_digest']}
        html = request('/', parse_json=False)
        if sha256(html) != manifest['files']['frontend/dist/index.html']['sha256']:
            raise RuntimeError('HTTP frontend differs from the bundled build')
        asset_paths = sorted(set(re.findall(r'(?:src|href)=["\']([^"\']+)["\']', html.decode())))
        frontend_assets = []
        for asset in asset_paths:
            parsed = urlsplit(urljoin(base + '/', asset))
            if parsed.netloc != urlsplit(base).netloc:
                raise RuntimeError('Packaged frontend requests an external startup asset')
            name = 'frontend/dist' + parsed.path
            if name not in manifest['files']:
                raise RuntimeError(f'Frontend asset absent from release inventory: {name}')
            response = request(parsed.path, parse_json=False)
            if sha256(response) != manifest['files'][name]['sha256']:
                raise RuntimeError(f'Frontend asset bytes changed: {name}')
            frontend_assets.append({'path': parsed.path, 'sha256': sha256(response)})
        if not any(asset['path'].endswith('.js') for asset in frontend_assets):
            raise RuntimeError('No browser JavaScript asset was served')
        result['frontend_assets'] = frontend_assets
        example = request('/api/examples/alpha101', {})
        context = request('/api/researches/' + example['research_id'] + '/workflow-observation-context')
        # The ordinary multi-candidate demo must not silently become the fixed
        # single-candidate human-comparison protocol.
        if context['compatibility']['compatible'] or context['selected'] is not None:
            raise RuntimeError('Observation context silently selected or changed the ordinary demo')
        if (context['runtime']['version'] != manifest['version']
                or context['runtime']['code_commit'] != manifest.get('source_commit')
                or context['runtime']['code_commit_verified'] != manifest.get('source_commit_verified', False)):
            raise RuntimeError('Observation defaults do not identify the actual portable source')
        observations = request('/api/researches/' + example['research_id'] + '/workflow-observations')
        if observations['total'] != 0:
            raise RuntimeError('Reading observation defaults unexpectedly created a record')
        (out / 'observation-context.json').write_bytes(json_bytes(context))
        result['observation_context'] = {'verified': True, 'source_commit': context['runtime']['code_commit'],
                                         'created_observations': 0, 'evidence': 'observation-context.json'}
        run = request('/api/runs', {'revision_id': example['revision_id'], 'mode': 'normalized_fixed',
                                    'idempotency_key': 'portable-source-acceptance'})
        run_id = run['id']
        deadline = time.monotonic() + timeout
        while True:
            run = request('/api/runs/' + run_id)
            if run['status'] in {'completed', 'failed', 'interrupted', 'cancelled'}:
                break
            if time.monotonic() > deadline or process.poll() is not None:
                raise RuntimeError('Relocated experiment timed out or service exited')
            time.sleep(.4)
        if run['status'] != 'completed' or not run.get('verification', {}).get('verified'):
            raise RuntimeError('Relocated experiment did not complete with verified artifacts')
        evaluated = [candidate for candidate in run['state']['candidates'] if candidate['status'] == 'evaluated']
        if len(evaluated) != 2:
            raise RuntimeError('Expected the two supported demonstration candidates to evaluate')
        report = request('/api/runs/' + run_id + '/report', parse_json=False)
        (out / 'report.md').write_bytes(report)
        result['experiment'] = {'run_id': run_id, 'status': run['status'], 'verification': run['verification'],
                                'evaluated_candidates': [candidate['id'] for candidate in evaluated],
                                'report_sha256': sha256(report)}
        (out / 'run.json').write_bytes(json_bytes(run))
        series_proofs = []
        for candidate in evaluated:
            target = next(item for item in run['review_targets'] if item['candidate_id'] == candidate['id'])
            series = request('/api/runs/' + run_id + '/candidates/' + candidate['id'] + '/series?attempt_id='
                             + target['attempt_id'] + '&result_digest=' + target['result_digest'])
            if (series['run_id'] != run_id or series['attempt_id'] != target['attempt_id']
                    or series['result_digest'] != target['result_digest'] or series['integrity'] != 'verified'
                    or series['summary']['evaluated_days'] != candidate['result']['metrics']['evaluated_days']
                    or not series['points'] or any(point['coverage'] is not None for point in series['points'] if point['status'] == 'purged')):
                raise RuntimeError('Relocated candidate series lost its frozen source or daily semantics')
            name = 'series-' + candidate['id'] + '.json'
            (out / name).write_bytes(json_bytes(series))
            series_proofs.append({'candidate_id': candidate['id'], 'evidence': name,
                                  'points': len(series['points']), 'artifact_sha256': series['source']['artifact_sha256']})
        result['candidate_series'] = {'verified': True, 'candidates': series_proofs}
        if browser_module is not None:
            result['browser'] = {'status': 'running'}
            browser_module = browser_module.resolve()
            # This driver belongs to the test host; the frontend and all server modules
            # it visits must still come from the extracted package.
            javascript = '''
const fs = require('node:fs');
const { chromium } = require(process.argv[1]);
(async () => {
  const browser = await chromium.launch(process.argv[3] === 'chrome' ? {channel:'chrome'} : {});
  const errors = []; const badResponses = [];
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    page.on('pageerror', error => errors.push(String(error)));
    page.on('response', response => { if(response.status() >= 400) badResponses.push({url:response.url(), status:response.status()}); });
    await page.goto(process.argv[2], {waitUntil:'networkidle'});
    await page.getByText('服务已连接', {exact:true}).waitFor({timeout:15000});
    await page.getByRole('button', {name:'导入示例研究', exact:true}).first().click();
    await page.getByRole('heading', {name:'alpha006', exact:true}).waitFor({timeout:15000});
    await page.screenshot({path:process.argv[4] + '/browser.png', fullPage:true});
    const proof = {url:page.url(), title:await page.title(), errors, badResponses};
    fs.writeFileSync(process.argv[4] + '/browser.json', JSON.stringify(proof, null, 2));
    if(errors.length || badResponses.length) throw new Error('Browser errors: ' + JSON.stringify(proof));
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode=1;});
'''
            command('browser-smoke', ['node', '-e', javascript, browser_module, base, browser_channel, out], limit=60)
            result['browser'] = {'status': 'passed', 'driver_module': str(browser_module),
                                 'channel': browser_channel, 'evidence': 'browser.json', 'screenshot': 'browser.png'}
        command('relocated-reference-suite', [python, '-m', 'paper_alpha', 'suite',
                '--manifest', source / 'evaluation_suites/v06/manifest.json', '--out', out / 'reference-suite'])
        command('relocated-reference-verification', [python, '-m', 'paper_alpha', 'verify-suite', out / 'reference-suite'])
        result['reference_suite'] = {'path': 'reference-suite', 'verified': True}
        command('relocated-mutation-recovery', [python, source / 'scripts/demo_v07.py', '--out', out / 'mutation-recovery'])
        recovery = json.loads((out / 'mutation-recovery/delivery.json').read_text())
        if not recovery['passed'] or not recovery['services_stopped']:
            raise RuntimeError('Relocated mutation recovery or owned service cleanup failed')
        result['mutation_recovery'] = {'path': 'mutation-recovery', 'verified': True}
        command('relocated-research-insights', [python, source / 'scripts/demo_v08.py', '--out', out / 'research-insights'])
        insights = json.loads((out / 'research-insights/delivery.json').read_text())
        if not insights['passed'] or not insights['services_stopped']:
            raise RuntimeError('Relocated research insights or owned service cleanup failed')
        result['research_insights'] = {'path': 'research-insights', 'verified': True}
        command('relocated-workflow-observations', [python, source / 'scripts/demo_v09.py', '--out', out / 'workflow-observations'])
        observations = json.loads((out / 'workflow-observations/delivery.json').read_text())
        if not observations['passed'] or not observations['services_stopped'] or observations['human_observations_added'] != 0:
            raise RuntimeError('Relocated observation flow, source isolation or owned service cleanup failed')
        result['workflow_observations'] = {'path': 'workflow-observations', 'verified': True}
        # Installing an editable project may create egg-info; the enumerated source
        # bytes themselves must not change while exercising the package.
        result['source_unchanged'] = all(sha256((source / name).read_bytes()) == item['sha256'] for name, item in manifest['files'].items())
        if not result['source_unchanged']:
            raise RuntimeError('Extracted release source changed during acceptance')
        result['passed'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        if result['browser']['status'] == 'running':
            result['browser']['status'] = 'failed'
    finally:
        if process is not None:
            try:
                result['launcher_exit_code'] = stop_process(process)
                result['launcher_stopped'] = process.poll() is not None
                if port is not None:
                    with socket.socket() as probe:
                        probe.settimeout(1)
                        result['service_port_closed'] = probe.connect_ex(('127.0.0.1', port)) != 0
                    if not result['service_port_closed']:
                        result['passed'] = False
                        result['cleanup_error'] = 'Owned service port is still reachable after shutdown'
            except Exception as exc:
                result['passed'] = False
                result['cleanup_error'] = str(exc)
        if log_stream:
            log_stream.close()
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        (out / 'result.json').write_bytes(json_bytes(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='New immutable evidence directory')
    parser.add_argument('--archive', type=Path, help='Existing allowlisted tar.gz; otherwise build the current source')
    parser.add_argument('--browser-module', type=Path, help='Optional installed Playwright module directory on the test host')
    parser.add_argument('--browser-channel', choices=('chrome', 'chromium'), default='chromium')
    parser.add_argument('--timeout', type=int, default=120, help='Seconds allowed for the real experiment')
    args = parser.parse_args(argv)
    if not 10 <= args.timeout <= 600:
        parser.error('--timeout must be within 10..600 seconds')
    result = run_check(args.out, args.archive, args.browser_module, args.browser_channel, args.timeout)
    print(json.dumps({'passed': result['passed'], 'result': str(args.out.resolve() / 'result.json'),
                      'error': result.get('error')}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
