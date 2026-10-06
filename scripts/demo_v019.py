"""Compose new-dir v19 engineering examples; never invent human or market results."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    result={'passed':False,'version':'0.19.0','scope':'Provider-free engineering examples; controlled synthetic inputs and explicit automation annotations. No real human labels, investment returns, paper replication or AI advantage claim.','started_at':datetime.now(timezone.utc).isoformat(),'llm_api_called':False,'human_annotations_written':0,'steps':[]}
    try:
        for name in ('lineage','domains','semantics'):
            log=out/(name+'.log');destination=out/name
            command=[sys.executable,str(ROOT/f'scripts/demo_v019_{name}.py'),'--out',str(destination)]
            with log.open('xb') as stream:
                process=subprocess.run(command,cwd=ROOT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},stdout=stream,stderr=subprocess.STDOUT,timeout=300)
            item={'name':name,'exit_code':process.returncode,'record':f'{name}/result.json','log':log.name}
            result['steps'].append(item)
            if process.returncode:raise RuntimeError(f'{name} failed; inspect {log}')
            report=json.loads((destination/'result.json').read_text())
            if report.get('passed') is not True:raise RuntimeError(f'{name} reported incomplete acceptance')
        result['passed']=True
    except Exception as exc:result['error']=f'{type(exc).__name__}: {exc}'
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'out':str(out)},ensure_ascii=False))
    return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
