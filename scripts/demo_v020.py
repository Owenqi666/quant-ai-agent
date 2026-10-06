"""Compose exact review/reference/binding examples in new directories."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    result = {'passed': False, 'version': '0.20.0',
        'scope': 'Engineering examples only. Automation declarations and controlled contract fixtures; no human judgment, model quality, paper reproduction or investment-performance claim.',
        'started_at': datetime.now(timezone.utc).isoformat(),
        'llm_api_called': False, 'human_records_written': 0, 'steps': []}
    try:
        for name in ('claim_reviews', 'semantic_sets', 'bindings'):
            log = out / (name + '.log')
            command = [sys.executable, str(ROOT / f'scripts/demo_v020_{name}.py'), '--out', str(out / name)]
            with log.open('xb') as stream:
                run = subprocess.run(command, cwd=ROOT, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                    stdout=stream, stderr=subprocess.STDOUT, timeout=300)
            result['steps'].append({'name': name, 'exit_code': run.returncode, 'log': log.name,
                'record': f'{name}/result.json'})
            if run.returncode:
                raise RuntimeError(f'{name} failed; inspect {log}')
            if json.loads((out / name / 'result.json').read_text()).get('passed') is not True:
                raise RuntimeError(f'{name} reported incomplete acceptance')
        result['passed'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'out': str(out)}, ensure_ascii=False))
    return 0 if result['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
