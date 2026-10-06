"""Validate explicitly supplied human declarations without rewriting the kit.

This offline control-plane entry records local declared identity. It cannot
authenticate a person or create an experiment/claim approval.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.evidence import sha256
from paper_alpha.storage import atomic_json, digest, read_json
from paper_alpha.semantic_materials import (
    MATERIAL, MANIFEST, Annotation, HumanDeclarations, DIMENSIONS,
    validate_declarations,
)


def confirm(input_path, out):
    input_path, out = Path(input_path).resolve(), Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / 'status.json', {'status': 'validating', 'passed': False})
    try:
        with input_path.open('rb') as stream:
            submitted_bytes = stream.read(256 * 1024 + 1)
        if len(submitted_bytes) > 256 * 1024:
            raise ValueError('Human declaration exceeds 256 KiB')
        (out / 'submitted-human-declarations.json').write_bytes(submitted_bytes)
        source_hash = sha256(MATERIAL)
        result = validate_declarations(read_json(out / 'submitted-human-declarations.json'))
        if sha256(MATERIAL) != source_hash:
            raise ValueError('Semantic material changed during declaration validation')
        shutil.copyfile(MATERIAL, out / 'frozen-semantic-material.json')
        if sha256(out / 'frozen-semantic-material.json') != source_hash:
            raise ValueError('Semantic material changed while copying its immutable evidence')
        body = {**result, 'submitted_input_sha256': hashlib.sha256(submitted_bytes).hexdigest()}
        identity = 'semantic_annotation_' + digest(body)
        atomic_json(out / 'result.json', {'id': identity, 'digest': digest(body), **body})
        lines = ['# Declared human semantic annotations', '', identity, '',
                 'Local declared identity; source-material labels only. Software did not authenticate the reviewer or verify semantic truth.', '',
                 '| Material case | Declared reviewer | Declared time | Declared status |', '|---|---|---|---|']
        lines += [f"| {row['case_id']} | {row['reviewer']} | {row['confirmed_at']} | {row['declared_semantic_status']} |" for row in body['annotations']]
        (out / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        atomic_json(out / 'status.json', {'status': 'completed', 'passed': True, 'scope': 'declaration_validation'})
        return {'id': identity, 'annotations': len(body['annotations']), 'result': str(out / 'result.json')}
    except Exception as exc:
        atomic_json(out / 'status.json', {'status': 'failed', 'passed': False, 'error_type': type(exc).__name__, 'error': str(exc)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True); parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(); result = confirm(args.input, args.out)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
