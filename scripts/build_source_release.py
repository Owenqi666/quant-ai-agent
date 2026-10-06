"""Build a deterministic, allowlisted source bundle for personal/local relocation.

The existing third-party paper is retained for the same user's local example.
Its redistribution rights are not established; this is not a public release step.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_alpha.source_identity import clean_checkout_commit
MANIFEST_NAME = 'RELEASE_MANIFEST.json'
# Files added outside these deliberately narrow inputs never enter a bundle.
REQUIRED_FILES = (
    'README.md', 'pyproject.toml', 'requirements-lock.txt', 'requirements-web-lock.txt', '.gitignore',
    '.github/workflows/ci.yml',
    'docs/backup_restore.md', 'docs/diagnostics.md', 'docs/fullstack_contract.md',
    'docs/import_manifest.json', 'docs/legacy_factor_audit.md', 'docs/project_audit.md',
    'docs/resume_evidence.md', 'docs/v0.4任务清单与Agent分工.md', 'docs/v0.5任务清单与Agent分工.md',
    'docs/v04_workflow.md', 'docs/workbench_design.md', 'docs/workbench_guide.md',
    'evaluation_suites/v04/README.md', 'evaluation_suites/v04/manifest.json',
    'evaluation_suites/v04/tasks/budget_stop.json', 'evaluation_suites/v04/tasks/extra_formulas.json',
    'evaluation_suites/v04/tasks/robustness.json', 'evaluation_suites/v04/tasks/wrong_quote.json',
    'evaluation_suites/v05/README.md', 'evaluation_suites/v05/manifest.json',
    'evaluation_suites/v05/task.json', 'evaluation_suites/v05/material_confirmation.json',
    'evaluation_suites/v05/human_review_template.json',
    'evaluation_suites/v06/README.md', 'evaluation_suites/v06/manifest.json',
    'evaluation_suites/v06/tasks/temporal_transforms.json', 'evaluation_suites/v06/tasks/unsupported_window.json',
    'evaluation_suites/v06/tasks/missing_field.json', 'evaluation_suites/v06/tasks/budget_stop.json',
    'docs/portable_release.md', 'docs/v05_api_contract.md',
    'docs/v0.6任务清单与Agent分工.md', 'docs/v06_research_review.md',
    'docs/v06_data_diagnostics.md', 'docs/v06_numerical_coverage.md',
    'docs/v0.7任务清单与Agent分工.md', 'docs/v07_idempotency.md',
    'docs/v07_review_recovery.md', 'docs/v07_workspace_state.md', 'docs/v07_human_evaluation.md',
    'evaluation_suites/v07/README.md', 'evaluation_suites/v07/human_protocol.json',
    'evaluation_suites/v07/human_observation_template.json',
    'docs/v0.8任务清单与Agent分工.md', 'docs/v08_submission.md',
    'docs/v08_research_insights.md', 'docs/v08_insights_ui.md', 'docs/v08_workflow.md',
    'docs/v0.9任务清单与Agent分工.md', 'docs/v09_observations.md', 'docs/v09_observations_ui.md',
    'docs/v09_workflow.md', 'docs/v09_research_readiness.md', 'docs/v09_final_test_protocol.md',
    'evaluation_suites/v09/readiness_real_data_template.json', 'evaluation_suites/v09/readiness_heldout_template.json',
    'docs/v0.10任务清单与Agent分工.md', 'docs/v010_workflow.md',
    'docs/v010_daily_workflow.md', 'docs/v010_observation_context.md', 'docs/v010_guided_observation.md',
    'docs/v0.11任务清单与Agent分工.md', 'docs/v011_review_charts.md', 'docs/v011_review_concurrency.md',
    'docs/v0.12任务清单与Agent分工.md', 'docs/v012_numerical.md', 'docs/v012_execution.md',
    'docs/v012_client_recovery.md', 'docs/v012_integration.md',
    'docs/v0.13任务清单与Agent分工.md', 'docs/v013_protocol_contract.md',
    'docs/v013_storage.md', 'docs/v013_numerical.md', 'docs/v013_workflow.md',
    'docs/v0.15任务清单与Agent分工.md', 'docs/v015_author_panel_contract.md',
    'docs/v015_workflow.md', 'docs/research/momentum_author_data_audit.md',
    'docs/v0.16任务清单与Agent分工.md', 'docs/v016_author_study_contract.md', 'docs/v016_workflow.md',
    'docs/v0.17任务清单与Agent分工.md', 'docs/v017_research_contract.md', 'docs/v017_case_contract.md',
    'docs/v017_tools.md', 'docs/v017_evaluation.md', 'docs/v017_workflow.md',
    'evaluation_suites/v017/manifest.json', 'evaluation_suites/v017/blocked_scan.json',
    'docs/v0.18任务清单与Agent分工.md', 'docs/v018_research_contract.md',
    'docs/v018_temporal_guard.md', 'docs/v018_execution.md', 'docs/v018_evaluation.md', 'docs/v018_workflow.md',
    'evaluation_suites/v018/manifest.json', 'evaluation_suites/v018/semantic_cases.json',
    'evaluation_suites/v018/human_review_template.json',
    'docs/v0.19任务清单与Agent分工.md', 'docs/v019_integration_contract.md',
    'docs/v019_observation_identity.md', 'docs/v019_domain_execution.md',
    'docs/v019_semantic_materials.md', 'docs/v019_workflow.md',
    'docs/v0.20任务清单与Agent分工.md', 'docs/v020_integration_contract.md',
    'docs/v020_claim_reviews.md', 'docs/v020_semantic_evaluation.md',
    'docs/v020_research_binding.md', 'docs/v020_workflow.md',
    'docs/v020_release_closeout.md', 'docs/v020_next_research_steps.md',
    'docs/v020_mom_only_plan.md', 'docs/v020_mom_only_workflow.md', 'docs/v020_mom_only_delivery.md',
    'docs/research/mom_only_method.md', 'docs/research/mom_only_contract.json',
    'examples/mom_only_industry/config.json',
    'docs/v021_mom_workbench_plan.md', 'docs/v021_mom_workbench_workflow.md',
    'docs/v022_research_acceptance_plan.md', 'docs/v022_human_review.md',
    'docs/v022_research_evaluation.md', 'docs/v022_ci_delivery.md',
    'docs/research/momentum_sources.json',
    'docs/research/momentum_eligibility_results.md',
    'examples/author_studies/intnl-mom.json', 'examples/author_studies/intnl-mom-dgw-mv.json',
    'examples/author_studies/us-mom.json', 'examples/author_studies/us-mom-dgw-mv.json',
    'docs/v0.14任务清单与Agent分工.md', 'docs/v014_monthly_contract.md',
    'docs/v014_numerical.md', 'docs/v014_execution.md', 'docs/v014_ui.md',
    'docs/v014_workflow.md', 'docs/v014_data_source_assessment.md',
    'scripts/build_source_release.py', 'scripts/check_portable_release.py',
    'scripts/check_clean_install.py', 'scripts/make_fixture.py',
    'paper_alpha/vendor/PROVENANCE.json',
    'examples/alpha101/paper.pdf', 'examples/alpha101/paper.json',
    'examples/alpha101/source_evidence.json', 'examples/alpha101/task.json',
    'examples/alpha101/evaluation.json', 'examples/alpha101/market.csv',
    'examples/alpha101/metadata.json',
    'tests/fixtures/datasets/README.md', 'tests/fixtures/datasets/generate.py',
    'tests/fixtures/datasets/market.csv', 'tests/fixtures/datasets/metadata.json',
    'tests/fixtures/datasets/research_config.json',
    'frontend/package.json', 'frontend/package-lock.json', 'frontend/index.html',
    'frontend/tsconfig.json',
    'frontend/vite.config.ts', 'frontend/dist/index.html', 'frontend/playwright.config.ts',
    'frontend/src/generated/openapi.json',
)
OPTIONAL_FILES = ('examples/alpha101/paper-page8.png', 'examples/alpha101/paper-page15.png')
SOURCE_TREES = {
    'paper_alpha': {'.py'},
    'scripts': {'.py'},
    'tests': {'.py'},
    'frontend/e2e': {'.ts'},
    'frontend/src': {'.ts', '.tsx', '.css', '.svg'},
    'frontend/dist/assets': {'.js', '.css', '.svg', '.woff', '.woff2', '.png'},
}
EXCLUDED_PARTS = {'__pycache__', '.git', 'node_modules', 'var', 'artifacts', 'private', '.venv'}
MAX_FILES = 2000
MAX_BYTES = 128 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def safe_relative(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and all(part not in {'', '.', '..'} for part in name.split('/')) and '\\' not in name and '\x00' not in name


def collect_files(root: Path) -> dict[str, bytes]:
    root = root.resolve()
    names = set(REQUIRED_FILES)
    names.update(name for name in OPTIONAL_FILES if (root / name).exists())
    for folder, extensions in SOURCE_TREES.items():
        base = root / folder
        if not base.is_dir() or base.is_symlink():
            raise ValueError(f'Required source directory missing or symlinked: {folder}')
        for path in base.rglob('*'):
            relative = path.relative_to(root)
            if any(part in EXCLUDED_PARTS or part.startswith('.') for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f'Symlink is not permitted in release inputs: {relative}')
            if path.is_file() and path.suffix in extensions:
                names.add(relative.as_posix())
    files = {}
    for name in sorted(names):
        path = root / name
        # Check every component, including selected directories, before reading.
        if not safe_relative(name) or any((root / Path(*Path(name).parts[:index])).is_symlink() for index in range(1, len(Path(name).parts) + 1)):
            raise ValueError(f'Unsafe or symlinked release input: {name}')
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError(f'Required release input missing or outside source: {name}')
        files[name] = path.read_bytes()
    if not any(name.startswith('paper_alpha/') and name.endswith('.py') for name in files):
        raise ValueError('Python runtime is empty')
    if not any(name.startswith('frontend/dist/assets/') and name.endswith('.js') for name in files):
        raise ValueError('Frontend build missing; run npm run build in frontend first')
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES:
        raise ValueError('Release inputs exceed bounded local bundle limits')
    # The paper provenance must match the bytes that will actually travel.
    paper = json.loads(files['examples/alpha101/paper.json'])
    evidence = json.loads(files['examples/alpha101/source_evidence.json'])
    paper_hash = sha256(files['examples/alpha101/paper.pdf'])
    if paper.get('document_sha256') != paper_hash or evidence.get('document_sha256') != paper_hash:
        raise ValueError('Bundled paper provenance does not match the original PDF')
    metadata = json.loads(files['examples/alpha101/metadata.json'])
    if metadata.get('data_kind') != 'synthetic':
        raise ValueError('Only the explicitly synthetic demonstration dataset may be bundled')
    return files


def create_bundle(root: Path, out: Path) -> dict:
    """Create a new evidence directory; never overwrite a previous package."""
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    try:
        return _write_bundle(root, out)
    except Exception as exc:
        (out / 'build.json').write_bytes(json_bytes({'passed': False, 'error': f'{type(exc).__name__}: {exc}'}))
        raise


def _write_bundle(root: Path, out: Path) -> dict:
    files = collect_files(root)
    project = tomllib.loads(files['pyproject.toml'].decode())['project']
    version = project['version']
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+-]{0,63}', version):
        raise ValueError('Package version is unsafe for an archive name')
    archive_root = f'paper-to-alpha-{version}'
    paper = json.loads(files['examples/alpha101/paper.json'])
    inventory = {name: {'size': len(data), 'sha256': sha256(data)} for name, data in files.items()}
    source_commit = clean_checkout_commit(root, files)
    manifest = {
        'schema_version': 1, 'format': 'personal-local-source-bundle',
        'project': project['name'], 'version': version, 'archive_root': archive_root,
        'files': inventory, 'content_sha256': sha256(json_bytes(inventory)),
        'source_commit': source_commit, 'source_commit_verified': source_commit is not None,
        'source_commit_scope': 'Local clean checkout and captured tracked blobs checked at build time; inventory includes generated assets. Not a signed or remote Git attestation.',
        'scope': 'Source and built browser assets for local/personal relocation; synthetic software demonstration. No public distribution or remote deployment is performed.',
        'paper_provenance': {
            'title': paper['title'], 'url': paper['url'], 'document_sha256': paper['document_sha256'],
            'redistribution_permission': 'not_verified',
            'note': 'Existing third-party paper and extracted text are retained for personal/local demonstration. Copyright and upstream rights remain with their owners; public redistribution requires a separate rights review.',
        },
        'exclusions': ['workspace databases and user uploads', 'artifacts and previous experiments',
                       'git history', 'dependency environments', 'secret/environment files', 'symlinks'],
    }
    manifest_bytes = json_bytes(manifest)
    archive = out / (archive_root + '.tar.gz')
    # Fixed metadata, sorting, gzip timestamp and filename make equal inputs byte-identical.
    with archive.open('wb') as stream:
        with gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as compressed:
            with tarfile.open(mode='w', fileobj=compressed, format=tarfile.PAX_FORMAT) as tar:
                for name, data in sorted({**files, MANIFEST_NAME: manifest_bytes}.items()):
                    info = tarfile.TarInfo(archive_root + '/' + name)
                    info.size, info.mode, info.mtime = len(data), 0o644, 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    tar.addfile(info, io.BytesIO(data))
    (out / MANIFEST_NAME).write_bytes(manifest_bytes)
    result = {'passed': True, 'archive': str(archive), 'archive_sha256': sha256(archive.read_bytes()),
              'content_sha256': manifest['content_sha256'], 'version': version,
              'file_count': len(files), 'format': manifest['format']}
    (out / 'build.json').write_bytes(json_bytes(result))
    return result


def extract_bundle(archive: Path, destination: Path) -> tuple[Path, dict]:
    """Verify every bounded member before writing; no tar extraction or links."""
    with tarfile.open(archive, 'r:gz') as tar:
        members = []
        total = 0
        for member in tar:
            if len(members) >= MAX_FILES + 1:
                raise ValueError('Too many archive members')
            if not member.isfile() or not safe_relative(member.name):
                raise ValueError(f'Unsafe archive member: {member.name}')
            total += member.size
            if member.size < 0 or total > MAX_BYTES:
                raise ValueError('Archive exceeds size limit')
            members.append(member)
        names = [member.name for member in members]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate archive member')
        manifests = [member for member in members if member.name.endswith('/' + MANIFEST_NAME)]
        if len(manifests) != 1 or manifests[0].size > 1024 * 1024:
            raise ValueError('Missing, duplicated or oversized release manifest')
        manifest = json.loads(tar.extractfile(manifests[0]).read())
        prefix = manifest.get('archive_root', '')
        if not safe_relative(prefix) or '/' in prefix or manifest.get('schema_version') != 1 or manifest.get('format') != 'personal-local-source-bundle':
            raise ValueError('Unsupported release manifest')
        inventory = manifest.get('files')
        if not isinstance(inventory, dict) or manifest.get('content_sha256') != sha256(json_bytes(inventory)):
            raise ValueError('Release inventory digest mismatch')
        if any(not safe_relative(name) or name == MANIFEST_NAME for name in inventory):
            raise ValueError('Unsafe inventory path')
        expected = {prefix + '/' + name for name in inventory} | {prefix + '/' + MANIFEST_NAME}
        if set(names) != expected:
            raise ValueError('Archive members differ from the exact release inventory')
        contents = {}
        for member in members:
            data = tar.extractfile(member).read()
            name = member.name.removeprefix(prefix + '/')
            if name != MANIFEST_NAME and inventory[name] != {'size': len(data), 'sha256': sha256(data)}:
                raise ValueError(f'Release file digest mismatch: {name}')
            contents[name] = data
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    source = destination / prefix
    for name, data in contents.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return source, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='New output directory; existing evidence is never replaced')
    args = parser.parse_args(argv)
    print(json.dumps(create_bundle(ROOT, args.out), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
