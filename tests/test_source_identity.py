"""Source identity must not inherit the host checkout or tolerate changed files."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from paper_alpha.source_identity import clean_checkout_commit, portable_source_commit


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def checkout(self):
        self.git('init')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / 'code.py').write_text('x=1\n')
        self.git('add', 'code.py')
        self.git('commit', '-m', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def bundle(self):
        (self.root / 'paper_alpha').mkdir()
        (self.root / 'paper_alpha/module.py').write_bytes(b'original')
        self.inventory = {'paper_alpha/module.py': {'size': 8, 'sha256': hashlib.sha256(b'original').hexdigest()}}
        self.manifest = {'schema_version': 1, 'format': 'personal-local-source-bundle',
                         'source_commit': 'a' * 40, 'source_commit_verified': True, 'files': self.inventory}
        self.write_manifest()

    def write_manifest(self):
        data = (json.dumps(self.inventory, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
        self.manifest['content_sha256'] = hashlib.sha256(data).hexdigest()
        (self.root / 'RELEASE_MANIFEST.json').write_text(json.dumps(self.manifest))

    def test_own_clean_checkout_only_and_no_parent_fallback(self):
        commit = self.checkout()
        self.assertEqual(clean_checkout_commit(self.root, {'code.py': b'x=1\n'}), commit)
        child = self.root / 'nested'
        child.mkdir()
        self.assertIsNone(clean_checkout_commit(child))
        self.assertIsNone(portable_source_commit(child))
        (self.root / 'code.py').write_text('x=2\n')
        self.assertIsNone(clean_checkout_commit(self.root))

    def test_captured_bytes_cannot_claim_another_commit(self):
        self.checkout()
        self.assertIsNone(clean_checkout_commit(self.root, {'code.py': b'changed'}))
        self.git('update-index', '--assume-unchanged', 'code.py')
        (self.root / 'code.py').write_bytes(b'changed')
        self.assertIsNone(clean_checkout_commit(self.root, {'code.py': b'changed'}))

    def test_verified_package_and_legacy_package(self):
        self.bundle()
        self.assertEqual(portable_source_commit(self.root), 'a' * 40)
        del self.manifest['source_commit_verified']
        self.write_manifest()
        self.assertIsNone(portable_source_commit(self.root))

    def test_tamper_missing_and_extra_module_reject_identity(self):
        self.bundle()
        file = self.root / 'paper_alpha/module.py'
        file.write_bytes(b'modified')
        self.assertIsNone(portable_source_commit(self.root))
        file.write_bytes(b'original')
        extra = self.root / 'paper_alpha/extra.py'
        extra.write_text('other')
        self.assertIsNone(portable_source_commit(self.root))
        extra.unlink()
        file.unlink()
        self.assertIsNone(portable_source_commit(self.root))

    def test_link_and_traversal_reject_identity(self):
        self.bundle()
        target = self.root / 'paper_alpha/module.py'
        target.rename(self.root / 'source.py')
        target.symlink_to('../source.py')
        self.assertIsNone(portable_source_commit(self.root))
        target.unlink()
        (self.root / 'source.py').rename(target)
        self.inventory['../outside.py'] = self.inventory.pop('paper_alpha/module.py')
        self.write_manifest()
        self.assertIsNone(portable_source_commit(self.root))

    def test_oversize_and_duplicate_manifest_reject_identity(self):
        self.bundle()
        self.inventory['paper_alpha/module.py']['size'] = 256 * 1024 * 1024
        self.write_manifest()
        self.assertIsNone(portable_source_commit(self.root))
        (self.root / 'RELEASE_MANIFEST.json').write_text('{"files":{},"files":{}}')
        self.assertIsNone(portable_source_commit(self.root))


if __name__ == '__main__':
    unittest.main()
