"""Register a strictly verified fixed MOM artifact in one local workbench.

Local administration only: no download, archived-code execution or human label.
The workspace keeps its own bytes; HTTP callers subsequently use an exact ID.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--artifact', type=Path, required=True)
    parser.add_argument('--title', default='49 行业组合 MOM · 固定真实来源开发案例')
    parser.add_argument('--note', default='User-selected industry-portfolio project modification; retrospective gross-only development example.')
    parser.add_argument('--idempotency-key', required=True)
    args = parser.parse_args(argv)
    from paper_alpha.server.industry_mom import IndustryMomSources
    from paper_alpha.server.maintenance import workspace_lease
    from paper_alpha.server.service import ServiceError, Store
    try:
        with workspace_lease(args.home.absolute()):
            store = Store(args.home.absolute())
            record = IndustryMomSources(store).register(args.artifact.absolute(), args.title, args.note, args.idempotency_key)
        print(json.dumps({'status': 'registered', 'source_id': record['id'],
            'source_digest': record['digest'], 'archive_sha256': record['archive_sha256'],
            'research_scope': record['research_scope'], 'human_judgment': None}, ensure_ascii=False))
        return 0
    except (ServiceError, ValueError, OSError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
