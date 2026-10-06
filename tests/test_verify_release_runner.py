"""Short real POSIX process controls; no application, source inputs or network."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_PATH = Path(__file__).resolve().parents[1] / 'scripts/verify_release.py'
_SPEC = importlib.util.spec_from_file_location('release_runner_under_test', _PATH)
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


@unittest.skipUnless(os.name == 'posix', 'Release process groups support macOS/Linux')
class ReleaseRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'logs'; self.out.mkdir()

    def run_stage(self, name, code, timeout=2):
        with patch.object(runner, 'ROOT', self.root):
            return runner.run_stage(name, [sys.executable, '-u', '-c', code], self.root,
                                    self.out, dict(os.environ), timeout)

    def check_log(self, record):
        log = self.out / record['log']
        self.assertEqual(hashlib.sha256(log.read_bytes()).hexdigest(), record['log_sha256'])
        before = log.read_bytes(); time.sleep(.05)
        self.assertEqual(log.read_bytes(), before)
        return before.decode()

    def test_only_python_suite_budget_changes(self):
        self.assertEqual(runner.stage_timeout_seconds('python-tests'), 1800)
        for name in ('api-contract', 'frontend-tests', 'browser-tests', 'portable-source'):
            self.assertEqual(runner.stage_timeout_seconds(name), 900)

    def test_real_success_and_nonzero_preserve_exit_and_log(self):
        for name, code, expected in [('success', "print('actual success')", 0),
                                    ('failure', "print('actual error'); raise SystemExit(7)", 7)]:
            record = self.run_stage(name, code)
            self.assertEqual(record['exit_code'], expected)
            self.assertFalse(record['timed_out'])
            self.assertEqual(record['timeout_seconds'], 2)
            self.assertNotIn('timeout_cleanup', record)
            self.assertIn('actual', self.check_log(record))

    def group_fixture(self):
        return '''
import json,signal,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-u','-c',"import time; print('owned child ready',flush=True); time.sleep(30)"])
def stop(signum,frame):
    child.terminate()
    child.wait(timeout=2)
    print('parent reaped child',flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
print(json.dumps({'child_pid':child.pid,'parent_pid':__import__('os').getpid()}),flush=True)
time.sleep(30)
'''

    def assert_group_fixture_closed(self, record):
        self.assertEqual(record['exit_code'], 124)
        self.assertTrue(record['timed_out'])
        self.assertEqual(record['timeout_seconds'], .4)
        self.assertTrue(record['timeout_cleanup']['leader_reaped'])
        self.assertLess(record['elapsed_seconds'], 4)
        text = self.check_log(record)
        identities = next(json.loads(line) for line in text.splitlines() if line.startswith('{'))
        self.assertIn('parent reaped child', text)
        for pid in identities.values():
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_real_timeout_cleans_owned_child_group_and_closes_log(self):
        record = self.run_stage('timeout-group', self.group_fixture(), timeout=.4)
        self.assertIn('SIGTERM', record['timeout_cleanup']['signals_sent'])
        self.assert_group_fixture_closed(record)

    def test_denied_group_fallback_records_scope_and_owned_fixtures_end(self):
        with patch.object(runner.os, 'killpg', side_effect=PermissionError(1, 'Controlled denial')):
            record = self.run_stage('denied-group', self.group_fixture(), timeout=.4)
        cleanup = record['timeout_cleanup']
        self.assertEqual(cleanup['group_cleanup_error'], {'signal': 'SIGTERM', 'type': 'PermissionError', 'errno': 1})
        self.assertEqual(cleanup['fallback_scope'], 'owned_unreaped_leader_only_group_cleanup_unconfirmed')
        self.assertIn('terminate', cleanup['leader_actions_sent'])
        self.assert_group_fixture_closed(record)

    def test_real_stubborn_leader_requires_owned_group_kill(self):
        record = self.run_stage('stubborn-leader',
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)",
            timeout=.4)
        self.assertEqual(record['exit_code'], 124)
        self.assertTrue(record['timed_out'])
        self.assertIn('SIGKILL', record['timeout_cleanup']['signals_sent'])
        self.assertEqual(record['process_exit_code'], -9)
        self.assertTrue(record['timeout_cleanup']['leader_reaped'])
        self.check_log(record)

    def test_main_retains_actual_timed_out_stage_before_failure_summary(self):
        destination = self.root / 'acceptance'
        observed = []; actual = runner.run_stage
        def short_control(name, command, cwd, out, env, timeout_seconds):
            observed.append((name, timeout_seconds))
            if name == 'api-contract':
                code, limit = "print('controlled API stage')", 2
            elif name == 'python-tests':
                code, limit = 'import time; time.sleep(30)', .2
            else:
                self.fail('A later stage ran after timeout')
            return actual(name, [sys.executable, '-u', '-c', code], cwd, out, env, limit)
        with patch.object(runner, 'ROOT', self.root), patch.object(runner, 'sources', return_value={}), \
             patch.object(runner, 'run_stage', side_effect=short_control), \
             patch.object(sys, 'argv', ['verify_release.py', '--out', str(destination)]), \
             patch.dict(os.environ, {'PLAYWRIGHT_PORT': '12345'}), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 1)
        result = json.loads((destination / 'acceptance.json').read_text())
        self.assertFalse(result['passed'])
        self.assertEqual(observed, [('api-contract', 900), ('python-tests', 1800)])
        self.assertEqual([step['name'] for step in result['steps']], ['api-contract', 'python-tests'])
        failed = result['steps'][1]
        self.assertEqual(failed['exit_code'], 124)
        self.assertTrue(failed['timed_out'])
        self.assertTrue(failed['timeout_cleanup']['leader_reaped'])
        self.assertEqual(hashlib.sha256((destination / failed['log']).read_bytes()).hexdigest(), failed['log_sha256'])
        self.assertGreater(failed['elapsed_seconds'], 0)
        self.assertIn('timeout', result['error'])


if __name__ == '__main__':
    unittest.main()
