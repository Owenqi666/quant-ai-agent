"""GET-only export failures, no overwrite, local bytes and exact target checks."""
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from paper_alpha.review_material_packet import load_bundle, safe_read, verify_packet
from paper_alpha.server.review_materials import ReviewMaterials
from paper_alpha.storage import digest, json_text
from scripts import prepare_case_review as export
from tests.test_human_review_packet import build_fixture

ROOT = Path(__file__).resolve().parents[1]


class PrepareCaseReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name).resolve(); cls.fixture = build_fixture(cls.root / 'daily')
        target = cls.fixture['targets'][1]; cls.args = (target['claims_id'], target['claim_id'], target['target_digest'])
        cls.packet, cls.contents = ReviewMaterials(cls.fixture['store'])._snapshot(*cls.args)

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=self.root)); self.out = self.directory / 'bundle'
        self.calls = []

    def request(self, client, method, path, **kwargs):
        self.assertEqual(method, 'GET'); self.calls.append(path); client.calls += 1
        if path == '/api/health': return deepcopy(self.fixture['health'])
        if path.startswith('/api/claim-review-targets/'): return deepcopy(self.packet['target'])
        if '/sources/' in path:
            source_id = path.split('/sources/')[1].split('?')[0]; return self.contents[source_id]
        return deepcopy(self.packet)

    def run_export(self, request=None):
        callback = request or self.request
        with patch.object(export.Client, 'request', lambda client, *args, **kwargs: callback(client, *args, **kwargs)):
            return export.prepare('http://127.0.0.1:8765', self.fixture['health']['workspace_id'], self.args[0], self.args[1], self.out)

    def rehash_manifest_file(self, filename, value):
        (self.out / filename).write_text(json_text(value))
        manifest = json.loads((self.out / 'manifest.json').read_text()); data = (self.out / filename).read_bytes()
        manifest['files'][filename] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
        manifest['digest'] = digest({k: v for k, v in manifest.items() if k != 'digest'})
        (self.out / 'manifest.json').write_text(json_text(manifest))

    def test_success_has_immutable_invocation_zero_human_and_get_only(self):
        result = self.run_export()
        self.assertTrue(result['passed']); self.assertTrue(result['source_files_verified']); self.assertEqual(result['business_writes'], 0)
        self.assertEqual(load_bundle(self.out / 'packet.json'), self.packet)
        self.assertEqual(result['human_records'], 0); self.assertFalse(result['llm_api_called'])
        worksheet = json.loads((self.out / 'blank-judgments.json').read_text())
        self.assertIsNone(worksheet['source']); self.assertIsNone(worksheet['reviewer']); self.assertIsNone(worksheet['confirmed_at'])
        self.assertTrue(all(v['outcome'] is None and v['reason'] is None for v in worksheet['dimensions'].values()))
        self.assertTrue((self.out / 'invocation.json').exists()); self.assertTrue((self.out / 'status.json').exists())
        self.assertFalse((self.out / 'failure.json').exists())
        self.assertEqual(self.fixture['store']._read('SELECT * FROM claim_reviews'), [])

    def test_first_get_failure_saves_attempt_input_and_never_valid_bundle(self):
        def fail(*args, **kwargs): raise ValueError('controlled disconnected GET')
        with self.assertRaises(ValueError): self.run_export(fail)
        self.assertTrue((self.out / 'invocation.json').exists())
        failed = json.loads((self.out / 'failure.json').read_text())
        self.assertEqual(failed['phase'], 'health'); self.assertFalse(failed['passed']); self.assertEqual(failed['human_judgments_written'], 0)
        with self.assertRaises((ValueError, OSError)): load_bundle(self.out)

    def test_wrong_workspace_saves_failure_without_further_get(self):
        def wrong(client, method, path, **kwargs):
            result = self.request(client, method, path, **kwargs); result['workspace_id'] = 'f' * 64; return result
        with self.assertRaises(ValueError): self.run_export(wrong)
        self.assertEqual(self.calls, ['/api/health'])
        self.assertEqual(json.loads((self.out / 'failure.json').read_text())['workspace_id_observed'], 'f' * 64)

    def test_material_projection_drift_saves_received_source_evidence(self):
        count = 0
        def drift(client, method, path, **kwargs):
            nonlocal count
            value = self.request(client, method, path, **kwargs)
            if path.startswith('/api/review-materials/') and '/sources/' not in path:
                count += 1
                if count == 2: value['digest'] = 'f' * 64
            return value
        with self.assertRaises(ValueError): self.run_export(drift)
        failed = json.loads((self.out / 'failure.json').read_text())
        self.assertEqual(failed['phase'], 'final_get_fence'); self.assertIn('alpha101-paper', failed['source_bytes_received'])
        self.assertFalse((self.out / 'manifest.json').exists())

    def test_existing_output_refused_before_any_get_and_preserved(self):
        self.out.mkdir(); (self.out / 'keep').write_text('original')
        with self.assertRaises(FileExistsError): self.run_export()
        self.assertEqual(self.calls, []); self.assertEqual((self.out / 'keep').read_text(), 'original')
        self.assertFalse((self.out / 'invocation.json').exists())

    def test_missing_extra_source_bytes_and_links_cannot_verify(self):
        self.run_export(); source = self.out / 'sources/alpha101-paper/alpha101-paper.pdf'; original = source.read_bytes()
        source.write_bytes(original + b'changed')
        with self.assertRaises(ValueError): load_bundle(self.out)
        source.write_bytes(original); (self.out / 'extra').write_text('unregistered')
        with self.assertRaises(ValueError): load_bundle(self.out)
        (self.out / 'extra').unlink(); source.unlink(); source.symlink_to(ROOT / 'examples/alpha101/paper.pdf')
        with self.assertRaises(ValueError): load_bundle(self.out)

    def test_rehashed_prefilled_worksheet_status_and_invocation_rejected(self):
        self.run_export()
        for name, field, value in [('blank-judgments.json', 'reviewer', 'Invented human'),
                                    ('status.json', 'passed', False), ('invocation.json', 'human_judgments_written', 1)]:
            saved = (self.out / name).read_text(); old_manifest = (self.out / 'manifest.json').read_text()
            changed = json.loads(saved); changed[field] = value; self.rehash_manifest_file(name, changed)
            with self.assertRaises(ValueError): load_bundle(self.out)
            (self.out / name).write_text(saved); (self.out / 'manifest.json').write_text(old_manifest)

    def test_rehashed_report_changed_number_still_rejected(self):
        self.run_export(); report = (self.out / 'report.md').read_text() + '\nInvented metric 999999.\n'
        (self.out / 'report.md').write_text(report)
        manifest = json.loads((self.out / 'manifest.json').read_text()); value = report.encode()
        manifest['files']['report.md'] = {'sha256': hashlib.sha256(value).hexdigest(), 'size': len(value)}
        manifest['digest'] = digest({k: v for k, v in manifest.items() if k != 'digest'}); (self.out / 'manifest.json').write_text(json_text(manifest))
        with self.assertRaises(ValueError): load_bundle(self.out)

    def test_rehashed_boolean_metadata_is_not_an_integer_version_or_count(self):
        self.run_export()
        manifest_original = (self.out / 'manifest.json').read_text()
        manifest = json.loads(manifest_original); manifest['schema_version'] = True
        manifest['digest'] = digest({k: v for k, v in manifest.items() if k != 'digest'})
        (self.out / 'manifest.json').write_text(json_text(manifest))
        with self.assertRaises(ValueError): load_bundle(self.out)
        (self.out / 'manifest.json').write_text(manifest_original)
        for filename, field, value in [('invocation.json', 'schema_version', True),
                                        ('status.json', 'business_writes', False)]:
            original = (self.out / filename).read_text(); changed = json.loads(original); changed[field] = value
            self.rehash_manifest_file(filename, changed)
            with self.assertRaises(ValueError): load_bundle(self.out)
            (self.out / filename).write_text(original); (self.out / 'manifest.json').write_text(manifest_original)

    def test_final_fence_rejects_new_link_to_directory(self):
        self.run_export()
        import paper_alpha.review_material_packet as library
        original, count = library.safe_read, 0
        def read(path, *args):
            nonlocal count
            data = original(path, *args)
            if Path(path).name == 'manifest.json':
                count += 1
                if count == 2: (self.out / 'late-link').symlink_to(self.directory, target_is_directory=True)
            return data
        with patch.object(library, 'safe_read', side_effect=read), self.assertRaises(ValueError):
            load_bundle(self.out)

    def test_unrelated_cwd_cli_and_library_do_not_require_scripts_import(self):
        self.run_export()
        result = subprocess.run([str(ROOT / '.venv/bin/python'), '-B', str(ROOT / 'scripts/prepare_case_review.py'), 'verify', str(self.out)],
                                cwd='/tmp', text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)['passed'])
        env = os.environ.copy(); env['PYTHONPATH'] = str(ROOT)
        result = subprocess.run([str(ROOT / '.venv/bin/python'), '-B', '-c',
            'import sys; from paper_alpha.review_material_packet import load_bundle; assert not any(k.startswith("scripts") for k in sys.modules)'],
            cwd='/tmp', env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ReviewMaterialClientTests(unittest.TestCase):
    def test_route_and_loopback_whitelist(self):
        for url in ('https://127.0.0.1:8765', 'http://example.com:80', 'http://localhost:8765', 'http://127.0.0.1:8765/x', 'http://user@127.0.0.1:8765'):
            with self.assertRaises(ValueError): export.Client(url)
        self.assertFalse(export.Client.allowed('POST', '/api/claim-reviews'))
        self.assertFalse(export.Client.allowed('GET', '/api/review-materials/research_claims_' + 'a' * 64 + '/sources/unknown?claim_id=x&expected_target_digest=' + 'b' * 64))
        self.assertFalse(export.Client.allowed('GET', '//other/api/health'))

    def test_actual_truncated_get_retries_and_redirect_refuses(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                calls.append(self.path)
                if self.server.redirect:
                    self.send_response(302); self.send_header('Location', 'http://example.com/'); self.end_headers(); return
                data = b'{"ok":true}'
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers()
                self.wfile.write(data[:3] if len(calls) == 1 else data); self.wfile.flush(); self.close_connection = True
            def do_POST(self): raise AssertionError('Exporter sent forbidden POST')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.redirect = False
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown); self.addCleanup(lambda: thread.join(timeout=1))
        client = export.Client(f'http://127.0.0.1:{server.server_port}')
        self.assertEqual(client.request('GET', '/api/health'), {'ok': True}); self.assertEqual(client.calls, 2)
        server.redirect = True
        with self.assertRaises(ValueError): client.request('GET', '/api/health')
        self.assertEqual(calls, ['/api/health'] * 3)

    def test_duplicate_json_and_nonfinite_rejected(self):
        for data in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'):
            with self.assertRaises(ValueError): export.decode(data)
