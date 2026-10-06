"""Offline CI preparation checks, never a remote workflow or public-release certificate.

Reads a bounded source/configuration snapshot and Git metadata names only. It does
not contact GitHub, inspect credentials, execute hooks, push or build the project.
An optional explicit metadata receipt is an observation, not authenticated proof.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'Owenqi666/quant-ai-agent'
TARGET_URL = 'https://github.com/' + TARGET
# Exact reviewed workflow, not a general YAML parser. A changed workflow requires review.
WORKFLOW_SHA256 = '8fe3365f66ea412bdb77da2693a1aeb2a057560299c188eb44b97bc8eb96da9e'
MAX_FILE = 8 * 1024 * 1024
MAX_TRACKED = 20000
REQUIRED = ('README.md', 'pyproject.toml', 'requirements-lock.txt', 'requirements-web-lock.txt',
            '.gitignore', '.github/workflows/ci.yml', 'frontend/package.json', 'frontend/package-lock.json',
            'frontend/playwright.config.ts', 'scripts/verify_release.py', 'scripts/check_portable_release.py',
            'scripts/build_source_release.py', 'scripts/check_ci_readiness.py', 'scripts/fetch_demo_paper.py', 'docs/v022_ci_delivery.md')
BOOTSTRAP_INPUTS = {'examples/alpha101/paper.pdf', 'examples/alpha101/paper.json'}


class CheckError(ValueError):
    pass


def safe_name(name):
    return (isinstance(name, str) and bool(name) and len(name) <= 512 and '\\' not in name
            and '\x00' not in name and not PurePosixPath(name).is_absolute()
            and all(part not in ('', '.', '..') for part in name.split('/')))


def read(root, name, limit=MAX_FILE):
    if not safe_name(name):
        raise CheckError('invalid_relative_path')
    parts = PurePosixPath(name).parts
    if any((root / Path(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)):
        raise CheckError('linked_input_rejected')
    file = root / name
    try:
        info = file.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CheckError('non_regular_or_hardlinked_input')
        if info.st_size > limit:
            raise CheckError('input_size_limit')
        value = file.read_bytes()
    except OSError:
        raise CheckError('required_input_unavailable') from None
    if len(value) > limit:
        raise CheckError('input_size_limit')
    return value


def parsed_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise CheckError('duplicate_json_key')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(CheckError('nonfinite_json')))
    except (ValueError, UnicodeError):
        raise CheckError('invalid_json') from None


def git_metadata(root):
    # No remote URL, credential helper, token, environment dump, Git config or hooks.
    prefix = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-C', str(root)]
    def run(args):
        try:
            result = subprocess.run(prefix + args, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise CheckError('git_metadata_unavailable') from None
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise CheckError('git_metadata_unavailable')
        return result.stdout
    try:
        if Path(run(['rev-parse', '--show-toplevel']).decode().strip()).resolve() != root:
            raise CheckError('git_root_mismatch')
        head = run(['rev-parse', 'HEAD']).decode().strip()
        if not re.fullmatch('[0-9a-f]{40}', head):
            raise CheckError('invalid_git_head')
        names = run(['remote']).splitlines() # Count only; deliberately never print names/URLs.
        tracked = run(['ls-files', '-z']).decode('utf-8').split('\x00')
        tracked = [name for name in tracked if name]
        if len(tracked) > MAX_TRACKED or any(not safe_name(name) for name in tracked):
            raise CheckError('tracked_inventory_limit_or_path')
        dirty = bool(run(['status', '--porcelain=v1', '--untracked-files=normal']))
        return {'status': 'available', 'head': head, 'remote_count': len(names), 'working_tree': 'dirty' if dirty else 'clean'}, tracked
    except (CheckError, UnicodeError):
        return {'status': 'not_available', 'head': None, 'remote_count': None, 'working_tree': 'not_checked'}, None


def private_category(name, *, public=False):
    parts = PurePosixPath(name).parts
    if parts[0] in {'artifacts', 'var', 'runs', 'backups', '.venv', '.aws', '.ssh', '.codex', '.agents'} or 'node_modules' in parts or 'private' in parts:
        return 'local_or_private_tree'
    file = parts[-1].lower()
    if public and (name in BOOTSTRAP_INPUTS or name.startswith('examples/alpha101/paper-page') or file.endswith('.pdf')):
        return 'third_party_paper_or_extracted_text'
    if file == '.env' or file.startswith('.env.') and file != '.env.example' or file in {'credentials', 'credentials.json', 'secrets.json', 'token.txt'}:
        return 'credential_filename'
    if file.endswith(('.pem', '.key', '.p12', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.mat', '.parquet', '.zip', '.xlsx')):
        return 'private_or_unregistered_binary'
    if file.endswith('.pdf') and name != 'examples/alpha101/paper.pdf':
        return 'unregistered_paper'
    if file.endswith('.csv') and name not in {'examples/alpha101/market.csv', 'tests/fixtures/datasets/market.csv'}:
        return 'unregistered_data'
    return None


def _policy(root):
    tree = ast.parse(read(root, 'scripts/build_source_release.py', 1024 * 1024).decode('utf-8'))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {'REQUIRED_FILES', 'OPTIONAL_FILES', 'SOURCE_TREES', 'EXCLUDED_PARTS'}:
                    values[target.id] = ast.literal_eval(node.value)
    if set(values) != {'REQUIRED_FILES', 'OPTIONAL_FILES', 'SOURCE_TREES', 'EXCLUDED_PARTS'}:
        raise CheckError('package_policy_unavailable')
    required, optional, trees, excluded = values['REQUIRED_FILES'], values['OPTIONAL_FILES'], values['SOURCE_TREES'], values['EXCLUDED_PARTS']
    if not isinstance(required, tuple) or not isinstance(optional, tuple) or not isinstance(trees, dict) or not isinstance(excluded, set) or any(not safe_name(name) for name in required + optional):
        raise CheckError('invalid_package_policy')
    if any(not safe_name(folder) or not isinstance(extensions, set) or any(not isinstance(extension, str) or not extension.startswith('.') for extension in extensions) for folder, extensions in trees.items()):
        raise CheckError('invalid_source_tree_policy')
    if not {'var', 'artifacts', 'private', '.git', '.venv', 'node_modules'} <= excluded:
        raise CheckError('package_private_boundary_changed')
    if 'docs/v022_ci_delivery.md' not in required or '.github/workflows/ci.yml' not in required or '.py' not in trees.get('scripts', set()):
        raise CheckError('new_delivery_inputs_not_packaged')
    return required, optional, trees, excluded


def package_policy(root, *, public=False):
    required, optional, trees, excluded = _policy(root)
    # Built dist is intentionally produced later by full acceptance, not needed in a fresh checkout.
    source_names = [name for name in required if not name.startswith('frontend/dist/') and not (public and name in BOOTSTRAP_INPUTS)]
    for name in source_names:
        read(root, name)
    return {'required_source_files': len(source_names), 'generated_frontend_build': 'produced_by_acceptance',
            'original_paper_and_text': 'explicit_fixed_source_bootstrap' if public else 'personal_local_material',
            'public_redistribution_rights': 'not_verified', 'third_party_industry_inputs': 'excluded_operator_owned'}


def verify_manifest(root, manifest):
    raw = read(manifest.parent.resolve(), manifest.name)
    value = parsed_json(raw)
    if not isinstance(value, dict) or not isinstance(value.get('files'), dict) or len(value['files']) > 2000:
        raise CheckError('invalid_release_manifest')
    inventory = value['files']
    canonical = (json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if value.get('schema_version') != 1 or value.get('format') != 'personal-local-source-bundle' or value.get('project') != 'paper-to-alpha' or value.get('content_sha256') != hashlib.sha256(canonical).hexdigest():
        raise CheckError('invalid_release_manifest_identity_or_inventory')
    required, optional, trees, excluded = _policy(root)
    expected = set(required) | {name for name in optional if (root / name).exists()}
    for folder, extensions in trees.items():
        base = root / folder
        if base.is_symlink() or not base.is_dir():
            raise CheckError('missing_or_linked_release_tree')
        for file in base.rglob('*'):
            name = file.relative_to(root)
            if any(part in excluded or part.startswith('.') for part in name.parts):
                continue
            if file.is_symlink():
                raise CheckError('linked_release_tree_input')
            if file.is_file() and file.suffix in extensions:
                expected.add(name.as_posix())
                if len(expected) > 2000:
                    raise CheckError('release_inventory_limit')
    if set(inventory) != expected:
        raise CheckError('release_manifest_inventory_mismatch')
    total = 0
    for name, item in value['files'].items():
        if not safe_name(name) or not isinstance(item, dict) or set(item) != {'size', 'sha256'} or type(item.get('size')) is not int or item['size'] < 0 or not re.fullmatch('[0-9a-f]{64}', str(item.get('sha256'))) or private_category(name):
            raise CheckError('manifest_private_boundary_or_record')
        body = read(root, name)
        total += len(body)
        if total > 128 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != item['sha256'] or len(body) != item['size']:
            raise CheckError('release_manifest_bytes_mismatch')
    version = tomllib.loads(read(root, 'pyproject.toml').decode())['project']['version']
    if value.get('version') != version:
        raise CheckError('release_manifest_version_mismatch')
    return {'files_checked': len(value['files']), 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'scope': 'Exact current package-policy inventory and bytes; not publisher authentication or public license approval.'}


def remote_observation(file):
    value = parsed_json(read(file.parent.resolve(), file.name, 64 * 1024))
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('repository') != TARGET or value.get('url') != TARGET_URL or value.get('observer') != 'gh-api-read-only':
        raise CheckError('remote_observation_target_mismatch')
    when = datetime.fromisoformat(value['checked_at'].replace('Z', '+00:00'))
    if when.tzinfo is None:
        raise CheckError('remote_observation_stale_or_invalid')
    age = (datetime.now(timezone.utc) - when).total_seconds()
    if not 0 <= age <= 86400 or not isinstance(value.get('permissions'), dict) or type(value['permissions'].get('push')) is not bool:
        raise CheckError('remote_observation_stale_or_invalid')
    if value.get('visibility') not in {'public', 'private'} or type(value.get('actions_total_count')) is not int or value['actions_total_count'] < 0 or not re.fullmatch('[0-9a-f]{40}', str(value.get('branch_head'))):
        raise CheckError('remote_observation_invalid')
    return {'status': 'push_permission_observed' if value['permissions']['push'] else 'push_permission_not_observed', 'checked_at': value['checked_at'],
            'visibility': value['visibility'], 'default_branch': value.get('default_branch'), 'branch_head': value['branch_head'],
            'observed_workflow_run_count': value['actions_total_count'], 'scope': 'Supplied metadata observation, no current network check or authentication of receipt.'}


def check(root=ROOT, *, manifest=None, observation=None, public=False):
    root = Path(root).resolve()
    report = {'schema_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'scope': 'Offline reviewed configuration/input preparation; no dependency installation, Linux run, credential scan or publication.',
              'remote_ci': 'not_run', 'model_execution': 'not_run', 'public_publish': 'not_performed', 'checks': [], 'file_sha256': {},
              'target_repository': TARGET, 'target_url': TARGET_URL, 'checkout_mode': 'public_export' if public else 'personal_local'}
    def step(name, function):
        try:
            data = function()
            report['checks'].append({'name': name, 'status': 'passed', 'details': data})
            return data
        except Exception as error:
            code = str(error) if isinstance(error, CheckError) else 'invalid_or_unavailable_input'
            report['checks'].append({'name': name, 'status': 'failed', 'code': code})
    def required():
        for name in REQUIRED:
            body = read(root, name)
            report['file_sha256'][name] = hashlib.sha256(body).hexdigest()
        return {'files_checked': len(REQUIRED)}
    step('required_ci_inputs', required)
    def workflow():
        if hashlib.sha256(read(root, '.github/workflows/ci.yml')).hexdigest() != WORKFLOW_SHA256:
            raise CheckError('workflow_changed_requires_review')
        return {'reviewed_workflow_sha256': WORKFLOW_SHA256, 'authority': 'contents_read', 'acceptance': 'full_isolated_software', 'job_timeout_minutes': 80,
                'full_acceptance_step_timeout_minutes': 70,
                'third_party_market_inputs': 'not_required', 'paper_bootstrap': 'fixed_original_url_sha_and_exact_extraction',
                'failure_evidence': 'only_acceptance_result_json_and_logs', 'scope': 'Exact reviewed workflow comparison; not general YAML validation or remote success.'}
    step('reviewed_workflow_contract', workflow)
    def versions():
        project = tomllib.loads(read(root, 'pyproject.toml').decode())['project']
        front = parsed_json(read(root, 'frontend/package.json')); lock = parsed_json(read(root, 'frontend/package-lock.json'))
        version = project['version']
        if not re.fullmatch(r'\d+\.\d+\.\d+', version) or version != front.get('version') or version != lock.get('version') or version != lock.get('packages', {}).get('', {}).get('version'):
            raise CheckError('project_frontend_lock_version_mismatch')
        if project.get('name') != 'paper-to-alpha' or front.get('name') != 'paper-alpha-workbench' or front.get('private') is not True:
            raise CheckError('package_identity_changed')
        for name in ('requirements-lock.txt', 'requirements-web-lock.txt'):
            for line in read(root, name, 64 * 1024).decode().splitlines():
                if line.strip() and not line.startswith('#') and line != '-r requirements-lock.txt' and not re.fullmatch(r'[a-zA-Z0-9_.-]+==[a-zA-Z0-9_.+-]+', line):
                    raise CheckError('dependency_lock_not_exact_local_pins')
        return {'version': version, 'python_job': '3.14', 'node_job': '24', 'dependencies': 'exact_version_locks', 'installed_consistency': 'not_checked'}
    report['versions'] = step('version_and_dependency_locks', versions)
    report['package_policy'] = step('source_package_policy', lambda: package_policy(root, public=public))
    metadata, tracked = git_metadata(root); report['git'] = metadata
    if tracked is None:
        report['checks'].append({'name': 'tracked_private_boundary', 'status': 'failed' if public else 'not_checked', 'code': 'no_verified_git_inventory'})
    else:
        def privacy():
            violations = {}
            for name in tracked:
                category = private_category(name, public=public)
                if category:
                    violations[category] = violations.get(category, 0) + 1
            if violations:
                report['tracked_private_violations'] = violations # Categories/counts only; never read secret contents.
                raise CheckError('tracked_private_or_unregistered_inputs')
            if public:
                ignore = read(root, '.gitignore', 64 * 1024).decode().splitlines()
                if not all(name in ignore or '/' + name in ignore for name in BOOTSTRAP_INPUTS):
                    raise CheckError('bootstrap_inputs_not_explicitly_gitignored')
            return {'tracked_file_count': len(tracked), 'contents_secret_scan': 'not_performed', 'scope': 'Known local/private/data filename boundary; not proof arbitrary source contains no secrets or redistribution is licensed.'}
        step('tracked_private_boundary', privacy)
    if manifest is not None:
        report['release_manifest'] = step('explicit_release_manifest', lambda: verify_manifest(root, Path(manifest)))
    else:
        report['release_manifest'] = {'status': 'not_requested', 'reason': 'Fresh checkout has no built release; full acceptance creates it.'}
    report['remote_access'] = step('supplied_remote_observation', lambda: remote_observation(Path(observation))) if observation is not None else {'status': 'not_checked'}
    report['passed'] = all(item['status'] != 'failed' for item in report['checks'])
    report['local_remote_status'] = 'not_configured' if metadata['remote_count'] == 0 else 'configured_target_not_checked' if metadata['remote_count'] else 'not_checked'
    report['target_access_ready'] = report['remote_access'] is not None and report['remote_access']['status'] == 'push_permission_observed'
    report['ready_to_dispatch'] = False # Target URL/ref and accepted code still require the coordinator's explicit remote action.
    report['remaining_remote_actions'] = ['verify_or_configure_exact_target_and_preserve_remote_history', 'commit_and_deliver_accepted_code', 'observe_exact_remote_workflow_run_and_evidence']
    if metadata['working_tree'] != 'clean':
        report['remaining_remote_actions'].insert(0, 'freeze_commit_after_local_acceptance')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--out', required=True, type=Path, help='A new evidence directory; never overwritten')
    parser.add_argument('--release-manifest', type=Path)
    parser.add_argument('--remote-observation', type=Path)
    parser.add_argument('--public-checkout', action='store_true', help='Reject tracked original PDF, extracted text and paper page images; require explicit bootstrap ignores')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    result = check(args.root, manifest=args.release_manifest, observation=args.remote_observation, public=args.public_checkout)
    (args.out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': result['passed'], 'remote_ci': 'not_run', 'target_access_ready': result['target_access_ready'], 'local_remote_status': result['local_remote_status'], 'evidence': str(args.out / 'result.json')}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
