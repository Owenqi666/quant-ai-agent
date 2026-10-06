"""Temporary actual ASGI contracts, bridged through isolated numeric-loopback HTTP.

The verified runtime commit is explicitly a patched test fixture. No actual
human judgment, existing workspace, published application, or port 8765 is used.
"""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import select
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from paper_alpha.server.api import create_app
from paper_alpha.server.db import connect
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.service import REPO, TASK_KEYS, Store
from paper_alpha.storage import read_json
from paper_alpha.workflow import run_task

sys.path.insert(0, str(REPO / 'scripts'))
import prepare_human_review as cli

TEST_COMMIT = '609ac9ba85d3c5215e2e3b79fa57f8dd8fa539fa'


class PreparationRuntimeTests(unittest.TestCase):
    def test_exact_runtime_pairs_support_upgrade_without_accepting_mixed_identity(self):
        value = {'status': 'ok', 'workspace_id': 'a' * 64, 'ai_enabled': False}
        for schema, version in ((16, '0.20.0'), (17, '0.21.0'), (17, '0.22.0')):
            cli._health({**value, 'database_schema': schema, 'version': version}, value['workspace_id'])
        for schema, version in ((16, '0.21.0'), (16, '0.22.0'), (17, '0.20.0'), (18, '0.22.0'), (True, '0.21.0')):
            with self.assertRaises(cli.PreparationError):
                cli._health({**value, 'database_schema': schema, 'version': version}, value['workspace_id'])


class Bridge:
    def __init__(self, app):
        self.client = TestClient(app)
        self.requests = []
        self.drop_create = False
        self.truncate_create = False
        self.redirect_health = False
        self.mutate = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.handle_request()

            def do_POST(self):
                self.handle_request()

            def handle_request(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                body = json.loads(raw) if raw else None
                owner.requests.append((self.command, self.path, body))
                if self.path == '/api/health' and owner.redirect_health:
                    self.send_response(302)
                    self.send_header('Location', 'http://example.invalid/secret')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                response = owner.client.request(self.command, self.path, content=raw or None,
                                                headers={'content-type': 'application/json'})
                if self.command == 'POST' and self.path == '/api/research-claims' and owner.drop_create:
                    # Real service has committed before the response disappears.
                    owner.drop_create = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                content = response.content
                if self.command == 'POST' and self.path == '/api/research-claims' and owner.truncate_create:
                    owner.truncate_create = False
                    self.send_response(response.status_code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(content) + 50))
                    self.end_headers()
                    self.wfile.write(content[:20])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if owner.mutate and self.command == 'GET' and response.headers.get('content-type', '').startswith('application/json'):
                    data = response.json()
                    owner.mutate(self.path, data)
                    content = json.dumps(data).encode()
                self.send_response(response.status_code)
                self.send_header('Content-Type', response.headers.get('content-type', 'application/json'))
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base_url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.client.close()


class PrepareHumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'workspace')
        paper = self.store.add_paper((REPO / 'examples/alpha101/paper.pdf').read_bytes(), 'Temporary fixed Alpha101 fixture')
        task = read_json(REPO / 'evaluation_suites/v05/task.json')
        task = {key: task[key] for key in TASK_KEYS}
        research = self.store.create_research('Temporary human preparation fixture', paper['id'], self.store.example_dataset_id, task)
        run = self.store.submit_run(research['latest_revision_id'], 'normalized_fixed', 'test-prepare-run')
        job = self.store.claim('test-prepare-worker')
        result = run_task(job['task_path'], job['output_dir'], mode='normalized_fixed')
        self.store.finish(run['id'], 'test-prepare-worker', job['attempt_id'], result['status'])
        cases = ResearchCases(self.store)
        preview = cases.preview('daily_run', run['id'])
        self.case = cases.create('Temporary fixed Case', 'Automated fixture only.', 'daily_run', run['id'], preview['source_digest'], 'test-case')
        self.workspace = sha256(str(self.store.root.resolve()).encode()).hexdigest()
        self.runtime_patch = patch('paper_alpha.server.observation_context.portable_source_commit', return_value=TEST_COMMIT)
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.bridge = Bridge(create_app(self.store.root))
        self.addCleanup(self.bridge.close)
        self.out = self.root / 'packet'

    def prepare(self, **changes):
        args = dict(base_url=self.bridge.base_url, workspace_id=self.workspace, case_id=self.case['id'],
                    out=self.out, server_commit=TEST_COMMIT, timeout=2, get_retries=0)
        args.update(changes)
        return cli.prepare(**args)

    def counts(self):
        tables = ('research_claims', 'research_claim_receipts', 'claim_reviews', 'semantic_annotations',
                  'semantic_evaluation_sets', 'workflow_observations', 'reviews', 'runs', 'research_cases')
        with closing(connect(self.store.db_path)) as connection:
            return {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in tables}

    def assert_only_drafts(self, before):
        after = self.counts()
        for table in before:
            if table not in {'research_claims', 'research_claim_receipts'}:
                self.assertEqual(after[table], before[table], table)
        posts = [path for method, path, _ in self.bridge.requests if method == 'POST']
        self.assertTrue(posts)
        self.assertEqual(set(posts), cli.ALLOWED_POSTS)

    def test_actual_http_preparation_offline_and_live_read_only(self):
        before = self.counts()
        result = self.prepare()
        self.assertTrue(result['passed'])
        self.assertEqual(result['human_judgments_generated'], 0)
        self.assertFalse(result['llm_api_called'])
        self.assertEqual(len(result['claim_ids']), 3)
        self.assert_only_drafts(before)
        checkpoint = len(self.bridge.requests)
        with patch.object(cli.Client, 'request', side_effect=AssertionError('Offline verification must not use HTTP')):
            self.assertTrue(cli.verify(self.out)['passed'])
            self.assertTrue(cli.resume(self.out)['passed'])
        self.assertEqual(len(self.bridge.requests), checkpoint)
        self.assertTrue(cli.verify(self.out, live=True)['passed'])
        self.assertTrue(all(method == 'GET' for method, _, _ in self.bridge.requests[checkpoint:]))
        status = json.loads((self.out / 'statuses.json').read_text())
        self.assertTrue(all(item['human_declared_status'] == 'pending' and item['human_records'] == 0
                            and item['semantic_quality_score'] is None for item in status))

    def test_lost_response_after_commit_original_key_resume(self):
        before = self.counts()
        self.bridge.drop_create = True
        with self.assertRaisesRegex(cli.PreparationError, 'Resume'):
            self.prepare()
        self.assertEqual(self.counts()['research_claims'], 1)
        self.assertEqual(self.counts()['research_claim_receipts'], 1)
        self.assertFalse((self.out / 'manifest.json').exists())
        request = (self.out / 'request.json').read_bytes()
        preparation = (self.out / 'preparation.json').read_bytes()
        self.assertTrue(cli.resume(self.out)['passed'])
        self.assertEqual((self.out / 'request.json').read_bytes(), request)
        self.assertEqual((self.out / 'preparation.json').read_bytes(), preparation)
        submitted = [body for method, path, body in self.bridge.requests
                     if method == 'POST' and path == '/api/research-claims']
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[0], submitted[1])
        self.assertEqual(self.counts()['research_claims'], 1)
        self.assertEqual(self.counts()['research_claim_receipts'], 1)
        self.assert_only_drafts(before)

    def test_wrong_workspace_rejected_before_other_reads_and_post(self):
        before = self.counts()
        with self.assertRaisesRegex(cli.PreparationError, 'identity mismatch'):
            self.prepare(workspace_id='0' * 64)
        self.assertEqual(self.bridge.requests, [('GET', '/api/health', None)])
        self.assertEqual(self.counts(), before)
        self.assertFalse(self.out.exists())

    def test_truncated_create_response_after_commit_can_resume(self):
        self.bridge.truncate_create = True
        with self.assertRaisesRegex(cli.PreparationError, 'Resume'):
            self.prepare()
        self.assertEqual(self.counts()['research_claims'], 1)
        self.assertTrue(cli.resume(self.out)['passed'])
        self.assertEqual(self.counts()['research_claim_receipts'], 1)

    def test_process_death_releases_lock_for_resume(self):
        self.bridge.drop_create = True
        with self.assertRaises(cli.PreparationError):
            self.prepare()
        code = ('import fcntl,os,sys,time; fd=os.open(sys.argv[1],os.O_RDWR); '
                'fcntl.flock(fd,fcntl.LOCK_EX); print("held",flush=True); time.sleep(30)')
        child = subprocess.Popen([sys.executable, '-c', code, str(self.out / '.prepare.lock')], stdout=subprocess.PIPE)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 3)[0])
            self.assertEqual(child.stdout.readline(), b'held\n')
            checkpoint = len(self.bridge.requests)
            with self.assertRaisesRegex(cli.PreparationError, 'Another preparation'):
                cli.resume(self.out)
            self.assertEqual(len(self.bridge.requests), checkpoint)
            child.kill()  # Only this test-owned lock holder, never the workbench.
            child.wait(timeout=3)
            self.assertTrue(cli.resume(self.out)['passed'])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()

    def test_wrong_server_commit_rejected_before_post(self):
        with self.assertRaisesRegex(cli.PreparationError, 'runtime'):
            self.prepare(server_commit='f' * 40)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.bridge.requests))

    def test_unverified_runtime_or_incompatible_protocol_rejected_before_post(self):
        for field in ('runtime', 'compatibility', 'selection'):
            def mutate(path, value):
                if '/workflow-observation-context' in path:
                    if field == 'runtime':
                        value['runtime']['code_commit_verified'] = False
                    elif field == 'compatibility':
                        value['compatibility']['compatible'] = False
                    else:
                        value['selected']['attempt_id'] = 'foreign-attempt'
            self.bridge.mutate = mutate
            with self.subTest(field=field), self.assertRaises(cli.PreparationError):
                self.prepare()
        self.assertFalse(any(method == 'POST' for method, _, _ in self.bridge.requests))

    def test_entire_protocol_and_output_contract_precedes_all_posts(self):
        variants = [('protocol', 'id', 'foreign'), ('protocol', 'digest', 'f' * 64),
                    ('protocol', 'task_sha256', 'e' * 64), ('compatibility', 'reason', 'Unexpected reason'),
                    ('selected', 'verification_error', 'Unverified output'),
                    ('outputs', 'task_sha256', 'e' * 64), ('outputs', 'run_id', 'foreign-run')]
        for container, key, changed in variants:
            def mutate(path, value):
                if '/workflow-observation-context' in path:
                    target = value['selected']['bound_outputs'] if container == 'outputs' else value[container]
                    target[key] = changed
            self.bridge.mutate = mutate
            with self.subTest(container=container, key=key), self.assertRaises(ValueError):
                self.prepare()
        self.assertFalse(any(method == 'POST' for method, _, _ in self.bridge.requests))
        self.assertEqual(self.counts()['research_claims'], 0)

    def test_resume_rechecks_entire_protocol_before_post(self):
        self.bridge.drop_create = True
        with self.assertRaises(cli.PreparationError):
            self.prepare()
        checkpoint = len(self.bridge.requests)
        def mutate(path, value):
            if '/workflow-observation-context' in path:
                value['protocol']['digest'] = 'f' * 64
        self.bridge.mutate = mutate
        with self.assertRaises(ValueError):
            cli.resume(self.out)
        self.assertTrue(all(method == 'GET' for method, _, _ in self.bridge.requests[checkpoint:]))
        self.assertEqual(self.counts()['research_claim_receipts'], 1)

    def test_partial_claim_temp_after_commit_resumes_exact_request(self):
        original_write = cli._write_temp
        def interrupt(descriptor, data, name):
            if name == 'claims.json':
                import os
                os.write(descriptor, b'{')
                raise OSError('Simulated process loss during output persistence')
            return original_write(descriptor, data, name)
        with patch.object(cli, '_write_temp', side_effect=interrupt), self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(self.counts()['research_claims'], 1)
        self.assertEqual((self.out / '.claims.json.tmp').read_bytes(), b'{')
        self.assertFalse((self.out / 'claims.json').exists())
        saved = (self.out / 'request.json').read_bytes()
        self.assertTrue(cli.resume(self.out)['passed'])
        self.assertEqual((self.out / 'request.json').read_bytes(), saved)
        self.assertFalse((self.out / '.claims.json.tmp').exists())
        submitted = [body for method, path, body in self.bridge.requests
                     if method == 'POST' and path == '/api/research-claims']
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[0], submitted[1])
        self.assertEqual(self.counts()['research_claims'], 1)
        self.assertEqual(self.counts()['research_claim_receipts'], 1)

    def test_death_after_atomic_publication_preserves_exact_final(self):
        original_sync = cli._sync_dir
        def interrupt(root):
            final = root / 'claims.json'
            if final.exists() and final.stat().st_nlink == 2:
                raise OSError('Simulated death between link publication and temporary unlink')
            return original_sync(root)
        with patch.object(cli, '_sync_dir', side_effect=interrupt), self.assertRaises(OSError):
            self.prepare()
        before = (self.out / 'claims.json').read_bytes()
        self.assertEqual((self.out / 'claims.json').stat().st_nlink, 2)
        self.assertTrue(cli.resume(self.out)['passed'])
        self.assertEqual((self.out / 'claims.json').read_bytes(), before)
        self.assertEqual((self.out / 'claims.json').stat().st_nlink, 1)
        self.assertEqual(self.counts()['research_claim_receipts'], 1)

    def test_unknown_temporary_entry_and_completed_corruption_not_overwritten(self):
        self.bridge.drop_create = True
        with self.assertRaises(cli.PreparationError):
            self.prepare()
        unknown = self.out / '.outside.json.tmp'
        unknown.write_text('{}')
        checkpoint = len(self.bridge.requests)
        with self.assertRaisesRegex(cli.PreparationError, 'Unknown output'):
            cli.resume(self.out)
        self.assertEqual(len(self.bridge.requests), checkpoint)
        self.assertTrue(unknown.exists())
        unknown.unlink()
        self.assertTrue(cli.resume(self.out)['passed'])
        completed = self.out / 'claims.json'
        completed.write_bytes(b'{')
        checkpoint = len(self.bridge.requests)
        with self.assertRaises(cli.PreparationError):
            cli.resume(self.out)
        self.assertEqual(completed.read_bytes(), b'{')
        self.assertEqual(len(self.bridge.requests), checkpoint)

    def test_redirect_and_nonlocal_url_refused(self):
        for url in ('https://127.0.0.1:8765', 'http://localhost:8765', 'http://example.com:8765',
                    'http://127.0.0.1:8765/x', 'http://127.0.0.1:8765?x=y', 'http://me@127.0.0.1:8765',
                    'http://127.0.0.1', 'http://127.0.0.1:8765/#x'):
            with self.subTest(url=url), self.assertRaises(cli.PreparationError):
                cli.normalize_url(url)
        self.bridge.redirect_health = True
        with self.assertRaisesRegex(cli.PreparationError, 'redirect'):
            self.prepare()
        self.assertEqual(len(self.bridge.requests), 1)

    def test_forbidden_routes_are_refused_without_network(self):
        client = cli.Client(self.bridge.base_url)
        for route in ('/api/claim-reviews', '/api/semantic-annotations', '/api/semantic-evaluation-sets',
                      '/api/runs', '/api/researches/x/workflow-observations', '/api/research-claims?redirect=x',
                      '/api/research-claims/preview/../reviews'):
            with self.subTest(route=route), self.assertRaises(cli.PreparationError):
                client.request('POST', route, {})
        with self.assertRaises(cli.PreparationError):
            client.request('GET', '//example.invalid/api/health')
        self.assertEqual(self.bridge.requests, [])

    def test_pdf_and_json_tampering_fail_offline_without_network(self):
        self.prepare()
        for filename in ('paper.pdf', 'request.json', 'inputs.json', 'claims.json', 'targets.json', 'statuses.json', 'packet.json'):
            path = self.out / filename
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.subTest(filename=filename), patch.object(cli.Client, 'request', side_effect=AssertionError('No network')):
                with self.assertRaises(cli.PreparationError):
                    cli.verify(self.out)
            path.write_bytes(original)
        self.assertTrue(cli.verify(self.out)['passed'])

    def test_original_request_cannot_be_replaced_during_resume(self):
        self.bridge.drop_create = True
        with self.assertRaises(cli.PreparationError):
            self.prepare()
        changed = json.loads((self.out / 'request.json').read_text())
        changed['claims'][0]['text'] += ' Changed.'
        (self.out / 'request.json').write_bytes(cli.canonical(changed))
        checkpoint = len(self.bridge.requests)
        with self.assertRaises(cli.PreparationError):
            cli.resume(self.out)
        self.assertEqual(len(self.bridge.requests), checkpoint)

    def test_no_overwrite_links_or_unexpected_export_entries(self):
        self.prepare()
        request = (self.out / 'request.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual((self.out / 'request.json').read_bytes(), request)
        extra = self.out / 'unknown.json'
        extra.write_text('{}')
        with self.assertRaisesRegex(cli.PreparationError, 'Unexpected'):
            cli.verify(self.out)
        extra.unlink()
        target = self.out / 'packet.json'
        saved = target.read_bytes()
        outside = self.root / 'outside.json'
        outside.write_bytes(saved)
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(cli.PreparationError, 'linked'):
            cli.verify(self.out)

    def test_cli_errors_are_closed_and_bounds_finite(self):
        with patch('builtins.print') as output:
            self.assertEqual(cli.main(['prepare', '--base-url', self.bridge.base_url, '--workspace-id', '0' * 64,
                                      '--case-id', self.case['id'], '--out', str(self.out)]), 1)
        self.assertFalse(json.loads(output.call_args.args[0])['passed'])
        for raw in (b'{"x":NaN}', b'{"x":1e999}', b'{"x":1,"x":2}', b'\xff'):
            with self.assertRaises(cli.PreparationError):
                cli.decode(raw)
        for timeout in (0, 31, float('nan'), True):
            with self.assertRaises(cli.PreparationError):
                cli.Client(self.bridge.base_url, timeout=timeout)
        for retries in (-1, 3, True):
            with self.assertRaises(cli.PreparationError):
                cli.Client(self.bridge.base_url, get_retries=retries)


if __name__ == '__main__':
    unittest.main()
