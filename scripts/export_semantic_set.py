"""Read/export one frozen semantic reference; does not initialize the workspace."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.semantic_evaluation import verify_bundle
from paper_alpha.server.semantic_evaluation_sets import SemanticEvaluationSets
from paper_alpha.storage import json_text


def export_set(workspace, set_id, destination):
    workspace = Path(workspace).resolve()
    database = workspace / 'workbench.sqlite3'
    if not database.is_file() or database.is_symlink() or (workspace / '.restore-incomplete').exists():
        raise ValueError('Use an existing complete upgraded workspace; export never initializes or migrates it')
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise ValueError('Export requires an explicit new file in an existing directory')
    if destination.resolve().is_relative_to(workspace):
        raise ValueError('Keep the exported bundle outside the authoritative workspace')
    service = SemanticEvaluationSets(SimpleNamespace(root=workspace, db_path=database))
    bundle = service.export(set_id)
    verified = verify_bundle(bundle, root=ROOT)
    # Exclusive creation preserves any prior export. No HTTP route accepts this
    # local destination and no new business/receipt record is generated.
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(json_text(bundle) + '\n')
    return {**verified, 'bundle': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--set-id', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(export_set(args.workspace, args.set_id, args.out)))
    except Exception as exc:
        print(json.dumps({'passed': False, 'error_type': type(exc).__name__, 'error': str(exc)}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
