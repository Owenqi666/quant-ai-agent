"""Bounded source completeness check; never downloads or certifies origins."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper_alpha.daily_workflow_v2 import _json
from paper_alpha.server.daily_source_qualification_v2 import validate_source_review

def main():
    p=argparse.ArgumentParser();p.add_argument('review');p.add_argument('--contract',required=True);p.add_argument('--receipt',required=True);a=p.parse_args()
    try:
        value=validate_source_review(_json(a.review),contract=_json(a.contract),receipt=_json(a.receipt));print(json.dumps(value,ensure_ascii=False));return 0 if value['checks_complete'] else 2
    except (ValueError,OSError,KeyError,TypeError):print(json.dumps({'status':'rejected','real_source_ready':False,'execution_allowed':False}));return 2
if __name__=='__main__':sys.exit(main())
