"""Small isolated configuration fixtures, never Linux/remote acceptance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_ci_readiness as tool


def fixture(root):
    names = tuple(tool.REQUIRED) + ('examples/alpha101/paper.pdf', 'examples/alpha101/paper.json', 'frontend/dist/index.html')
    for name in names:
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'synthetic configuration fixture\n')
    (root / '.github/workflows/ci.yml').write_bytes((tool.ROOT / '.github/workflows/ci.yml').read_bytes())
    (root / '.gitignore').write_text('artifacts/\nvar/\nexamples/alpha101/paper.pdf\nexamples/alpha101/paper.json\n')
    (root / 'pyproject.toml').write_text('[project]\nname="paper-to-alpha"\nversion="0.22.0"\n')
    front = {'name': 'paper-alpha-workbench', 'private': True, 'version': '0.22.0'}
    (root / 'frontend/package.json').write_text(json.dumps(front))
    (root / 'frontend/package-lock.json').write_text(json.dumps({'version': '0.22.0', 'packages': {'': front}}))
    (root / 'requirements-lock.txt').write_text('fixture==1.0\n')
    (root / 'requirements-web-lock.txt').write_text('-r requirements-lock.txt\nfixture-web==1.0\n')
    excluded = {'var', 'artifacts', 'private', '.git', '.venv', 'node_modules', '__pycache__'}
    (root / 'scripts/build_source_release.py').write_text('REQUIRED_FILES = ' + repr(names) + '\nOPTIONAL_FILES = ()\nSOURCE_TREES = {"scripts": {".py"}}\nEXCLUDED_PARTS = ' + repr(excluded) + '\n')
    return names


def metadata(names):
    return {'status': 'available', 'head': '1' * 40, 'remote_count': 0, 'working_tree': 'clean'}, list(names)


def manifest(root):
    required, _, trees, _ = tool._policy(root)
    names = set(required)
    for folder, extensions in trees.items():
        names.update(file.relative_to(root).as_posix() for file in (root / folder).rglob('*') if file.is_file() and file.suffix in extensions)
    inventory = {name: {'size': len((root / name).read_bytes()), 'sha256': hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in sorted(names)}
    value = {'schema_version': 1, 'format': 'personal-local-source-bundle', 'project': 'paper-to-alpha', 'version': '0.22.0',
             'files': inventory, 'content_sha256': hashlib.sha256((json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()).hexdigest()}
    path = root / 'RELEASE_MANIFEST.json'; path.write_text(json.dumps(value)); return path


class CIReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.root = Path(self.directory.name)
        self.names = fixture(self.root)
        self.git = patch.object(tool, 'git_metadata', return_value=metadata(self.names)); self.git.start()

    def tearDown(self):
        self.git.stop(); self.directory.cleanup()

    def failure(self, result, name):
        self.assertFalse(result['passed'])
        self.assertEqual(next(item for item in result['checks'] if item['name'] == name)['status'], 'failed')

    def test_local_ready_is_never_remote_success(self):
        value = tool.check(self.root)
        self.assertTrue(value['passed'])
        self.assertEqual(value['remote_ci'], 'not_run')
        self.assertFalse(value['ready_to_dispatch'])
        self.assertFalse(value['target_access_ready'])
        self.assertEqual(value['local_remote_status'], 'not_configured')
        self.assertEqual(value['package_policy']['public_redistribution_rights'], 'not_verified')

    def test_public_checkout_before_bootstrap_accepts_missing_untracked_materials(self):
        for name in tool.BOOTSTRAP_INPUTS:
            (self.root / name).unlink()
        names = [name for name in self.names if name not in tool.BOOTSTRAP_INPUTS]
        with patch.object(tool, 'git_metadata', return_value=metadata(names)):
            value = tool.check(self.root, public=True)
        self.assertTrue(value['passed'])
        self.assertEqual(value['checkout_mode'], 'public_export')

    def test_public_checkout_rejects_tracked_pdf_fulltext_and_images(self):
        names = [name for name in self.names if name not in tool.BOOTSTRAP_INPUTS]
        for material in ['examples/alpha101/paper.pdf', 'examples/alpha101/paper.json', 'examples/alpha101/paper-page8.png', 'other.pdf']:
            with self.subTest(material=material), patch.object(tool, 'git_metadata', return_value=metadata(names + [material])):
                self.failure(tool.check(self.root, public=True), 'tracked_private_boundary')

    def test_public_requires_explicit_bootstrap_ignores(self):
        (self.root / '.gitignore').write_text('*.pdf\n')
        names = [name for name in self.names if name not in tool.BOOTSTRAP_INPUTS]
        with patch.object(tool, 'git_metadata', return_value=metadata(names)):
            self.failure(tool.check(self.root, public=True), 'tracked_private_boundary')

    def test_public_accepts_root_anchored_explicit_ignores(self):
        (self.root / '.gitignore').write_text('/examples/alpha101/paper.pdf\n/examples/alpha101/paper.json\n')
        names = [name for name in self.names if name not in tool.BOOTSTRAP_INPUTS]
        with patch.object(tool, 'git_metadata', return_value=metadata(names)):
            self.assertTrue(tool.check(self.root, public=True)['passed'])

    def test_unavailable_git_inventory_is_explicit_and_public_fails(self):
        with patch.object(tool, 'git_metadata', return_value=({'status': 'not_available', 'head': None, 'remote_count': None, 'working_tree': 'not_checked'}, None)):
            self.assertTrue(tool.check(self.root)['passed'])
            self.failure(tool.check(self.root, public=True), 'tracked_private_boundary')

    def test_changed_workflow_requires_review(self):
        with (self.root / '.github/workflows/ci.yml').open('a') as stream:
            stream.write('# unreviewed change\n')
        self.failure(tool.check(self.root), 'reviewed_workflow_contract')

    def test_versions_and_dependency_locks_rejected_on_drift(self):
        path = self.root / 'frontend/package.json'; original = path.read_bytes()
        value = json.loads(original); value['version'] = '0.21.0'; path.write_text(json.dumps(value))
        self.failure(tool.check(self.root), 'version_and_dependency_locks')
        path.write_bytes(original)
        (self.root / 'requirements-lock.txt').write_text('fixture>=1.0\n')
        self.failure(tool.check(self.root), 'version_and_dependency_locks')

    def test_missing_delivery_doc_and_changed_exclusion_are_rejected(self):
        path = self.root / 'docs/v022_ci_delivery.md'; path.unlink()
        self.failure(tool.check(self.root), 'required_ci_inputs')
        path.write_text('fixture')
        builder = self.root / 'scripts/build_source_release.py'
        builder.write_text(builder.read_text().replace("'artifacts',", ''))
        self.failure(tool.check(self.root), 'source_package_policy')

    def test_private_tracked_names_are_counts_only_no_content_read(self):
        names = self.names + ('var/secret.sqlite3', 'artifacts/source.zip', '.env.secret', 'token.txt')
        with patch.object(tool, 'git_metadata', return_value=metadata(names)):
            result = tool.check(self.root)
        self.failure(result, 'tracked_private_boundary')
        self.assertNotIn('token.txt', json.dumps(result))
        self.assertEqual(sum(result['tracked_private_violations'].values()), 4)

    def test_linked_required_input_rejected(self):
        path = self.root / 'README.md'; path.unlink(); path.symlink_to(self.root / '.gitignore')
        self.failure(tool.check(self.root), 'required_ci_inputs')

    def test_exact_manifest_bytes_and_full_inventory(self):
        path = manifest(self.root)
        result = tool.check(self.root, manifest=path)
        self.assertTrue(result['passed'])
        (self.root / 'README.md').write_text('mutated')
        self.failure(tool.check(self.root, manifest=path), 'explicit_release_manifest')

    def test_manifest_missing_record_and_incorrect_size_rejected(self):
        for mutation in ('missing', 'size'):
            with self.subTest(mutation=mutation):
                path = manifest(self.root); value = json.loads(path.read_bytes())
                if mutation == 'missing':
                    del value['files']['README.md']
                else:
                    value['files']['README.md']['size'] = True
                value['content_sha256'] = hashlib.sha256((json.dumps(value['files'], ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()).hexdigest()
                path.write_text(json.dumps(value))
                self.failure(tool.check(self.root, manifest=path), 'explicit_release_manifest')

    def test_observed_push_permission_still_not_remote_dispatch(self):
        path = self.root / 'observation.json'
        value = {'schema_version': 1, 'observer': 'gh-api-read-only', 'repository': tool.TARGET, 'url': tool.TARGET_URL,
                 'checked_at': datetime.now(timezone.utc).isoformat(), 'permissions': {'push': True}, 'visibility': 'public',
                 'default_branch': 'main', 'branch_head': '2' * 40, 'actions_total_count': 0}
        path.write_text(json.dumps(value))
        result = tool.check(self.root, observation=path)
        self.assertTrue(result['passed']); self.assertTrue(result['target_access_ready'])
        self.assertFalse(result['ready_to_dispatch']); self.assertEqual(result['remote_ci'], 'not_run')
        for field, wrong in [('repository', 'someone/else'), ('checked_at', (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()),
                             ('checked_at', datetime.now().isoformat()), ('permissions', {'push': 'yes'})]:
            with self.subTest(field=field, wrong=wrong):
                malformed = dict(value); malformed[field] = wrong; path.write_text(json.dumps(malformed))
                self.failure(tool.check(self.root, observation=path), 'supplied_remote_observation')

    def test_invalid_json_duplicate_keys_and_paths_are_rejected(self):
        with self.assertRaises(tool.CheckError):
            tool.parsed_json(b'{"x":1,"x":2}')
        with self.assertRaises(tool.CheckError):
            tool.parsed_json(b'{"x":NaN}')
        for name in ('../outside', '/absolute', 'a//b', 'a\\b', 'a/./b'):
            self.assertFalse(tool.safe_name(name))


if __name__ == '__main__':
    unittest.main()
