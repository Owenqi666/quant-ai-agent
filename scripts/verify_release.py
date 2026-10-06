"""One repeatable local/CI acceptance entrypoint; evidence is never overwritten."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def sources():
    files = [ROOT / name for name in ('README.md', 'pyproject.toml', 'requirements-lock.txt', 'requirements-web-lock.txt', '.gitignore')]
    for folder in ('paper_alpha', 'tests', 'scripts', 'docs', 'examples', 'evaluation_suites', '.github', 'frontend'):
        for path in (ROOT / folder).rglob('*'):
            relative = path.relative_to(ROOT)
            if (path.is_file() and not any(part in {'__pycache__', 'node_modules', 'dist', 'test-results', 'playwright-report'} for part in relative.parts)
                    and path.suffix not in {'.pyc', '.tsbuildinfo'} and path.name != '.DS_Store'):
                files.append(path)
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(files))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path, help='A new directory for immutable acceptance logs/results')
    parser.add_argument('--browser-channel', choices=('chrome', 'chromium'), default='chromium')
    parser.add_argument('--industry-mom-artifact', type=Path,
                        help='Optional operator-owned fixed artifact for a separate real-source development flow')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    start = sources()
    summary = {
        'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
        'python': platform.python_version(), 'platform': platform.platform(),
        'scope': 'Local synthetic software acceptance. No AI, human efficiency or investment-performance claim. Remote CI status is not inferred.',
        'source_sha256': start,
        'release_sha256': hashlib.sha256(json.dumps(start, sort_keys=True).encode()).hexdigest(),
        'steps': [], 'passed': False,
    }
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    # Choose a test port without disturbing unrelated local services.
    if not env.get('PLAYWRIGHT_PORT'):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            env['PLAYWRIGHT_PORT'] = str(probe.getsockname()[1])
    summary['browser_channel'] = args.browser_channel
    summary['browser_test_port'] = int(env['PLAYWRIGHT_PORT'])
    if args.browser_channel == 'chrome':
        env['PLAYWRIGHT_CHANNEL'] = 'chrome'
    else:
        env.pop('PLAYWRIGHT_CHANNEL', None)
    commands = [
        ('api-contract', [sys.executable, str(ROOT / 'scripts/export_api_contract.py'), '--check'], ROOT),
        ('python-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], ROOT),
        ('frontend-build', ['npm', 'run', 'build'], ROOT / 'frontend'),
        ('frontend-tests', ['npm', 'test'], ROOT / 'frontend'),
        ('browser-tests', ['npm', 'run', 'test:e2e'], ROOT / 'frontend'),
        ('baseline', [sys.executable, '-m', 'paper_alpha', 'benchmark', '--task', str(ROOT / 'examples/alpha101/task.json'), '--out', str(out / 'baseline')], ROOT),
        ('reference-evaluation', [sys.executable, '-m', 'paper_alpha', 'suite', '--manifest', str(ROOT / 'evaluation_suites/v04/manifest.json'), '--out', str(out / 'reference-evaluation')], ROOT),
        ('reference-verification', [sys.executable, '-m', 'paper_alpha', 'verify-suite', str(out / 'reference-evaluation')], ROOT),
        ('selected-paper-evaluation', [sys.executable, '-m', 'paper_alpha', 'suite', '--manifest', str(ROOT / 'evaluation_suites/v05/manifest.json'), '--out', str(out / 'selected-paper-evaluation')], ROOT),
        ('selected-paper-verification', [sys.executable, '-m', 'paper_alpha', 'verify-suite', str(out / 'selected-paper-evaluation')], ROOT),
        ('expanded-numerical-evaluation', [sys.executable, '-m', 'paper_alpha', 'suite', '--manifest', str(ROOT / 'evaluation_suites/v06/manifest.json'), '--out', str(out / 'expanded-numerical-evaluation')], ROOT),
        ('expanded-numerical-verification', [sys.executable, '-m', 'paper_alpha', 'verify-suite', str(out / 'expanded-numerical-evaluation')], ROOT),
        ('research-quality-flow', [sys.executable, str(ROOT / 'scripts/demo_v06.py'), '--out', str(out / 'research-quality-flow')], ROOT),
        ('commit-response-recovery', [sys.executable, str(ROOT / 'scripts/demo_v07.py'), '--out', str(out / 'commit-response-recovery')], ROOT),
        ('research-insights-flow', [sys.executable, str(ROOT / 'scripts/demo_v08.py'), '--out', str(out / 'research-insights-flow')], ROOT),
        ('workflow-observations-flow', [sys.executable, str(ROOT / 'scripts/demo_v09.py'), '--out', str(out / 'workflow-observations-flow')], ROOT),
        ('research-protocol-flow', [sys.executable, str(ROOT / 'scripts/demo_v013.py'), '--out', str(out / 'research-protocol-flow')], ROOT),
        ('author-panel-flow', [sys.executable, str(ROOT / 'scripts/demo_v015.py'), '--out', str(out / 'author-panel-flow')], ROOT),
        ('author-study-flow', [sys.executable, str(ROOT / 'scripts/demo_v016.py'), '--out', str(out / 'author-study-flow')], ROOT),
        ('research-task-flow', [sys.executable, str(ROOT / 'scripts/demo_v017.py'), '--out', str(out / 'research-task-flow')], ROOT),
        ('research-decision-evaluation', [sys.executable, str(ROOT / 'scripts/evaluate_v017.py'), '--out', str(out / 'research-decision-evaluation')], ROOT),
        ('research-execution-flow', [sys.executable, str(ROOT / 'scripts/demo_v018.py'), '--out', str(out / 'research-execution-flow')], ROOT),
        ('referenced-research-evaluation', [sys.executable, str(ROOT / 'scripts/evaluate_v018.py'), '--out', str(out / 'referenced-research-evaluation')], ROOT),
        ('cross-domain-lineage-semantics', [sys.executable, str(ROOT / 'scripts/demo_v019.py'), '--out', str(out / 'v019-flow')], ROOT),
        ('exact-review-reference-binding', [sys.executable, str(ROOT / 'scripts/demo_v020.py'), '--out', str(out / 'v020-flow')], ROOT),
        ('research-acceptance-samples', [sys.executable, '-B', str(ROOT / 'scripts/demo_v022_research_acceptance.py'),
             '--out', str(out / 'research-acceptance-samples')] +
             (['--industry-mom-artifact', str(args.industry_mom_artifact.absolute())]
              if args.industry_mom_artifact is not None else []), ROOT),
        ('monthly-experiment-flow', [sys.executable, str(ROOT / 'scripts/demo_v014.py'), '--out', str(out / 'monthly-experiment-flow')], ROOT),
        ('dataset-feedback-restore', [sys.executable, str(ROOT / 'scripts/demo_v04.py'), '--out', str(out / 'dataset-flow')], ROOT),
        ('workspace-diagnostics', [sys.executable, '-m', 'paper_alpha.server.diagnostics', '--home', str(out / 'dataset-flow/restored'), '--out', str(out / 'diagnostics.json')], ROOT),
        ('workbench-measurement', [sys.executable, str(ROOT / 'scripts/measure_workbench.py'), '--out', str(out / 'performance')], ROOT),
        ('review-concurrency', [sys.executable, str(ROOT / 'scripts/measure_review_concurrency.py'), '--out', str(out / 'review-concurrency')], ROOT),
        ('execution-claim-concurrency', [sys.executable, str(ROOT / 'scripts/measure_execution_claim.py'), '--implementation', 'current', '--out', str(out / 'execution-claim-concurrency')], ROOT),
        ('regression-concurrency', [sys.executable, str(ROOT / 'scripts/measure_regression_concurrency.py'), '--out', str(out / 'regression-concurrency')], ROOT),
        ('portable-source', [sys.executable, str(ROOT / 'scripts/check_portable_release.py'), '--out', str(out / 'portable-source'),
                             '--browser-module', str(ROOT / 'frontend/node_modules/playwright'), '--browser-channel', args.browser_channel], ROOT),
    ]
    if args.industry_mom_artifact is not None:
        artifact = args.industry_mom_artifact.absolute()
        summary['industry_source_input'] = {
            'path_declaration': str(artifact),
            'manifest_sha256': hashlib.sha256((artifact / 'manifest.json').read_bytes()).hexdigest(),
            'scope': 'Optional exact real industry development input; no human label or reserved evaluation.'}
        commands.append(('industry-mom-workbench-flow', [sys.executable,
            str(ROOT / 'scripts/demo_v021_mom_workbench.py'), '--artifact', str(artifact),
            '--out', str(out / 'industry-mom-workbench-flow')], ROOT))
    try:
        for name, command, cwd in commands:
            print(f'Running {name}; log: {out / (name + ".log")}', flush=True)
            started = time.monotonic()
            log = out / (name + '.log')
            with log.open('wb') as stream:
                process = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=900)
            summary['steps'].append({'name': name, 'argv': command, 'cwd': str(cwd.relative_to(ROOT)),
                                     'exit_code': process.returncode, 'elapsed_seconds': time.monotonic() - started,
                                     'log': log.name, 'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()})
            if process.returncode:
                raise RuntimeError(f'{name} failed; inspect {log}')
        summary['source_unchanged'] = sources() == start
        if not summary['source_unchanged']:
            raise RuntimeError('Source changed during acceptance; preserve this run and rerun into a new directory')
        summary['passed'] = True
    except Exception as exc:
        summary['error'] = str(exc)
        print(str(exc), file=sys.stderr)
    finally:
        summary['finished_at'] = datetime.now(timezone.utc).isoformat()
        (out / 'acceptance.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': summary['passed'], 'release_sha256': summary['release_sha256'], 'evidence': str(out / 'acceptance.json')}))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
