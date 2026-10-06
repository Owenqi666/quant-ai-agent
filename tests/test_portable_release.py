"""Archive boundary and repeatability checks; no network or subprocess installs."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_release_under_test', ROOT / 'scripts/build_source_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class PortableReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        for name in release.REQUIRED_FILES:
            self.write(name, b'fixture')
        self.write('pyproject.toml', b'[project]\nname="paper-to-alpha"\nversion="0.5.0"\n')
        self.write('paper_alpha/__init__.py', b'')
        self.write('frontend/e2e/workflow.spec.ts', b'// fixture')
        self.write('frontend/src/main.tsx', b'export const fixture = true;')
        self.write('frontend/dist/assets/main.js', b'console.log("fixture")')
        pdf = b'fixture-pdf-bytes'
        self.write('examples/alpha101/paper.pdf', pdf)
        self.write('examples/alpha101/paper.json', release.json_bytes({'title': 'Existing fixture',
                   'url': 'https://example.invalid/paper', 'document_sha256': release.sha256(pdf)}))
        self.write('examples/alpha101/source_evidence.json', release.json_bytes({'document_sha256': release.sha256(pdf)}))
        self.write('examples/alpha101/metadata.json', b'{"data_kind":"synthetic"}')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def build(self, name='built'):
        return release.create_bundle(self.root, self.base / name)

    def rewrite(self, source, changes=None, additions=()):
        target = self.base / 'modified.tar.gz'
        with tarfile.open(source, 'r:gz') as original, tarfile.open(target, 'w:gz') as changed:
            for member in original.getmembers():
                data = original.extractfile(member).read()
                if changes:
                    data = changes(member.name, data)
                    member.size = len(data)
                changed.addfile(member, io.BytesIO(data))
            for member, data in additions:
                changed.addfile(member, io.BytesIO(data) if data is not None else None)
        return target

    def test_same_inputs_produce_identical_archive_and_safe_roundtrip(self):
        first = self.build()
        for path in self.root.rglob('*'):
            if path.is_file():
                path.touch()
        second = self.build('second')
        self.assertEqual(first['archive_sha256'], second['archive_sha256'])
        source, manifest = release.extract_bundle(Path(first['archive']), self.base / 'extract')
        self.assertEqual(manifest['content_sha256'], first['content_sha256'])
        self.assertEqual(manifest['paper_provenance']['redistribution_permission'], 'not_verified')
        for name, item in manifest['files'].items():
            self.assertEqual(release.sha256((source / name).read_bytes()), item['sha256'])

    def test_workspace_secrets_dependencies_and_unselected_papers_are_excluded(self):
        excluded = ('var/workbench/research.sqlite3', 'artifacts/old-run.json', '.env', '.git/config',
                    'frontend/node_modules/private.js', 'paper_alpha/.env', 'paper_alpha/private/key.py',
                    'paper_alpha/key.pem', 'examples/private.pdf', 'examples/alpha101/new-private.pdf')
        for name in excluded:
            self.write(name, b'MUST_NOT_TRAVEL')
        built = self.build()
        manifest = json.loads((Path(built['archive']).parent / release.MANIFEST_NAME).read_text())
        self.assertFalse(set(excluded) & set(manifest['files']))
        with tarfile.open(built['archive']) as archive:
            self.assertFalse(any(archive.extractfile(member).read() == b'MUST_NOT_TRAVEL' for member in archive))

    def test_symlinked_file_and_selected_parent_are_rejected(self):
        outside = self.base / 'outside.py'
        outside.write_text('private')
        (self.root / 'paper_alpha/leak.py').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            self.build()
        (self.root / 'paper_alpha/leak.py').unlink()
        (self.root / 'README.md').unlink()
        (self.root / 'README.md').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlinked'):
            self.build('second')

    def test_missing_frontend_and_mismatched_paper_provenance_fail(self):
        (self.root / 'frontend/dist/assets/main.js').unlink()
        with self.assertRaisesRegex(ValueError, 'Frontend build missing'):
            self.build()
        failure = json.loads((self.base / 'built/build.json').read_text())
        self.assertFalse(failure['passed'])
        self.assertIn('Frontend build missing', failure['error'])
        self.write('frontend/dist/assets/main.js', b'javascript')
        self.write('examples/alpha101/paper.pdf', b'changed')
        with self.assertRaisesRegex(ValueError, 'provenance'):
            self.build('second')

    def test_file_tampering_does_not_extract_anything(self):
        built = self.build()
        archive = self.rewrite(built['archive'], lambda name, data: b'changed' if name.endswith('/README.md') else data)
        destination = self.base / 'bad-extract'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            release.extract_bundle(archive, destination)
        self.assertFalse(destination.exists())

    def test_traversal_symlinks_hardlinks_duplicates_and_extra_files_are_rejected(self):
        built = self.build()
        prefix = 'paper-to-alpha-0.5.0/'
        for name, kind in (('../escape', 'file'), (prefix + 'link', 'sym'),
                           (prefix + 'hard', 'hard'), (prefix + 'README.md', 'file'),
                           (prefix + 'extra.txt', 'file')):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                data = b'unexpected'
                if kind == 'sym':
                    member.type, member.linkname, data = tarfile.SYMTYPE, '/tmp/outside', None
                elif kind == 'hard':
                    member.type, member.linkname, data = tarfile.LNKTYPE, prefix + 'README.md', None
                else:
                    member.size = len(data)
                archive = self.rewrite(built['archive'], additions=[(member, data)])
                with self.assertRaises(ValueError):
                    release.extract_bundle(archive, self.base / 'bad-extract')
                self.assertFalse((self.base / 'bad-extract').exists())

    def test_existing_build_or_extract_evidence_is_never_overwritten(self):
        built = self.build()
        with self.assertRaises(FileExistsError):
            self.build()
        release.extract_bundle(Path(built['archive']), self.base / 'extract')
        with self.assertRaises(FileExistsError):
            release.extract_bundle(Path(built['archive']), self.base / 'extract')


if __name__ == '__main__':
    unittest.main()
