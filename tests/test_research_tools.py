"""Permission boundaries and durable budget behavior in real isolated stores."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from paper_alpha import eligibility
from paper_alpha.server.db import transaction
from paper_alpha.server.service import Store
from paper_alpha.server.author_studies import AuthorStudies
from paper_alpha.server.backup import create_backup, restore_backup
from paper_alpha.server.research_cases import ResearchCases
from paper_alpha.server.research_tools import ResearchTools, ToolServiceError
from paper_alpha.server.research_tools_schema import ToolCallCreate, ToolSession, ToolCapabilities
from paper_alpha.storage import digest


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'home')
        self.studies = AuthorStudies(self.store)
        scan = eligibility.demo_scan()
        scan['plan']['minimum_assets'] = 100
        scan['plan_digest'] = digest(scan['plan'])
        self.study = self.studies.create('Synthetic study', 'Fixture; not raw-source verification.',
                                         scan, None, [], 'study')
        cases = ResearchCases(self.store)
        preview = cases.preview('author_study', self.study['id'])
        self.case = cases.create('Research case', 'Fixture context', 'author_study', self.study['id'],
                                 preview['source_digest'], 'case')
        self.tools = ResearchTools(self.store)

    def session(self, **budget):
        return self.tools.create(self.case['id'], self.case['digest'],
                                 {'max_calls': 10, 'max_errors': 5, 'max_seconds': 60, **budget}, 'session')

    def proposal(self, **changes):
        return {'action': 'stop_data_insufficient', 'rationale': 'Insufficient declared coverage; preserve the result.',
                'evidence_ids': [item['id'] for item in self.case['context']['evidence']], **changes}

    def test_capabilities_are_closed_and_provider_free(self):
        value = ToolCapabilities.model_validate(self.tools.capabilities()).model_dump()
        self.assertFalse(value['provider_connected'])
        self.assertEqual({item['name'] for item in value['tools']},
                         {'read_case', 'read_evidence', 'read_result', 'propose_next_action'})
        self.assertIn('write_human_review', value['forbidden'])
        self.assertIn('access_final_test', value['forbidden'])

    def test_reads_preserve_original_context_and_scientific_stop_is_success(self):
        session = self.session()
        for tool, field, expected in [('read_case', 'case_json', self.case),
                                     ('read_evidence', 'evidence_json', self.case['context']['evidence']),
                                     ('read_result', 'result_json', self.case['context']['results'])]:
            record = self.tools.call(session['id'], tool, {}, tool)
            self.assertEqual(record['status'], 'completed'); self.assertTrue(record['response']['ok'])
            self.assertEqual(json.loads(record['response'][field]), expected)
        restored = ToolSession.model_validate(self.tools.get(session['id'])).model_dump()
        self.assertEqual(restored['usage']['calls'], 3); self.assertEqual(restored['usage']['errors'], 0)
        self.assertEqual(self.case['context']['state'], 'data_insufficient')

    def test_proposal_is_only_unverified_automation_and_cannot_approve(self):
        session = self.session()
        before = deepcopy(self.studies.get(self.study['id']))
        call = self.tools.call(session['id'], 'propose_next_action', self.proposal(), 'draft')
        draft = call['response']['proposal']
        self.assertEqual((draft['actor'], draft['status'], draft['semantic_fidelity']), ('automation', 'draft', 'unverified'))
        self.assertEqual(self.studies.reviews(self.study['id']), {'items': []})
        self.assertEqual(self.studies.get(self.study['id']), before)
        denied = self.tools.call(session['id'], 'propose_next_action', self.proposal(actor='human'), 'spoof')
        self.assertEqual(denied['response']['error']['code'], 'ARGUMENTS_INVALID')

    def test_denied_capabilities_and_fabricated_numbers_are_recorded(self):
        session = self.session(max_calls=20, max_errors=20)
        for tool in ('import_scan', 'write_human_review', 'lower_threshold', 'execute_author_portfolio', 'final_test', 'python', 'read_file'):
            value = self.tools.call(session['id'], tool, {'path': '/etc/passwd', 'metrics': {'sharpe': 9}}, tool)
            self.assertEqual(value['response']['error']['code'], 'TOOL_DENIED')
            self.assertFalse(value['response']['error']['retryable'])
        value = self.tools.call(session['id'], 'propose_next_action', self.proposal(metrics={'sharpe': 9}), 'metrics')
        self.assertEqual(value['response']['error']['code'], 'ARGUMENTS_INVALID')
        self.assertEqual(self.tools.get(session['id'])['usage']['errors'], 8)

    def test_action_and_evidence_must_belong_to_frozen_case(self):
        session = self.session()
        wrong_action = self.tools.call(session['id'], 'propose_next_action', self.proposal(action='resolve_method'), 'action')
        self.assertEqual(wrong_action['response']['error']['code'], 'ACTION_NOT_ALLOWED')
        wrong_evidence = self.tools.call(session['id'], 'propose_next_action', self.proposal(evidence_ids=['invented']), 'evidence')
        self.assertEqual(wrong_evidence['response']['error']['code'], 'EVIDENCE_NOT_FOUND')
        duplicate = self.proposal(evidence_ids=['invented', 'invented'])
        self.assertEqual(self.tools.call(session['id'], 'propose_next_action', duplicate, 'duplicates')['response']['error']['code'], 'ARGUMENTS_INVALID')

    def test_create_and_call_replay_do_not_reset_or_double_charge(self):
        session = self.session(max_calls=1)
        first = self.tools.call(session['id'], 'read_result', {}, 'read')
        restarted = ResearchTools(Store(self.store.root))
        self.assertEqual(restarted.call(session['id'], 'read_result', {}, 'read'), first)
        replay = restarted.create(self.case['id'], self.case['digest'], session['budget'], 'session')
        self.assertEqual(replay['usage']['calls'], 1); self.assertEqual(replay['status'], 'exhausted')
        with self.assertRaises(ToolServiceError) as raised:
            restarted.call(session['id'], 'read_case', {}, 'new')
        self.assertEqual(raised.exception.code, 'BUDGET_EXHAUSTED')
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(restarted.get(session['id'])['usage']['calls'], 1)

    def test_changed_idempotency_requests_conflict(self):
        session = self.session()
        self.tools.call(session['id'], 'read_case', {}, 'read')
        with self.assertRaises(ToolServiceError) as raised:
            self.tools.call(session['id'], 'read_result', {}, 'read')
        self.assertEqual(raised.exception.code, 'IDEMPOTENCY_CONFLICT')
        with self.assertRaises(ToolServiceError):
            self.tools.create(self.case['id'], self.case['digest'], {**session['budget'], 'max_calls': 20}, 'session')
        self.assertEqual(self.tools.get(session['id'])['usage']['calls'], 1)

    def test_backup_restore_preserves_receipts_drafts_and_budget(self):
        session = self.session(max_calls=2)
        first = self.tools.call(session['id'], 'read_result', {}, 'result')
        self.tools.call(session['id'], 'propose_next_action', self.proposal(), 'draft')
        before = self.tools.get(session['id'])
        root = Path(self.temp.name).resolve()
        create_backup(self.store.root, root / 'backup')
        restore_backup(root / 'backup', root / 'restored')
        restored = ResearchTools(Store(root / 'restored'))
        self.assertEqual(restored.get(session['id']), before)
        self.assertEqual(restored.call(session['id'], 'read_result', {}, 'result'), first)
        with self.assertRaises(ToolServiceError) as raised:
            restored.call(session['id'], 'read_case', {}, 'new')
        self.assertEqual(raised.exception.code, 'BUDGET_EXHAUSTED')

    def test_error_budget_prevents_unbounded_invalid_retries(self):
        session = self.session(max_errors=1)
        failed = self.tools.call(session['id'], 'unknown', {}, 'bad')
        self.assertEqual(self.tools.call(session['id'], 'unknown', {}, 'bad'), failed)
        self.assertEqual(self.tools.get(session['id'])['status'], 'exhausted')
        with self.assertRaises(ToolServiceError) as raised:
            self.tools.call(session['id'], 'read_case', {}, 'new')
        self.assertEqual(raised.exception.code, 'BUDGET_EXHAUSTED')

    def test_interrupted_call_stays_charged_after_restart_and_new_key(self):
        session = self.session(max_seconds=30)
        with patch.object(self.tools, '_dispatch', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.tools.call(session['id'], 'read_result', {}, 'interrupted')
        before = self.tools.get(session['id'])
        self.assertEqual(before['status'], 'running'); self.assertEqual(before['usage']['elapsed_seconds'], 30)
        restarted = ResearchTools(Store(self.store.root))
        with self.assertRaises(ToolServiceError) as raised:
            restarted.call(session['id'], 'read_case', {}, 'new')
        self.assertEqual(raised.exception.code, 'BUDGET_EXHAUSTED')
        after = restarted.get(session['id'])
        self.assertEqual(after['status'], 'exhausted'); self.assertEqual(after['calls'][0]['status'], 'interrupted')
        replay = restarted.call(session['id'], 'read_result', {}, 'interrupted')
        self.assertEqual(replay['response']['error']['code'], 'CALL_INTERRUPTED')
        self.assertEqual(after['usage']['elapsed_seconds'], 30)

    def test_late_response_is_discarded_and_budget_persisted(self):
        session = self.session(max_seconds=1)
        with patch('paper_alpha.server.research_tools.time.monotonic', side_effect=[100.0, 102.0]):
            call = self.tools.call(session['id'], 'read_result', {}, 'slow')
        self.assertEqual(call['response']['error']['code'], 'TIME_BUDGET_EXHAUSTED')
        self.assertIsNone(call['response']['result_json'])
        self.assertEqual(self.tools.get(session['id'])['usage']['elapsed_seconds'], 2)

    def test_process_exit_releases_owner_and_preserves_charged_reservation(self):
        session = self.session(max_seconds=30)
        script = '''
import os, sys
from pathlib import Path
from unittest.mock import patch
from paper_alpha.server.service import Store
from paper_alpha.server.research_tools import ResearchTools
tools = ResearchTools(Store(Path(sys.argv[1])))
with patch.object(tools, '_dispatch', side_effect=lambda *args: os._exit(23)):
    tools.call(sys.argv[2], 'read_result', {}, 'process-exit')
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.store.root), session['id']],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 23, result.stderr)
        restored = ResearchTools(Store(self.store.root))
        call = restored.call(session['id'], 'read_result', {}, 'process-exit')
        self.assertEqual(call['status'], 'interrupted')
        self.assertEqual(call['response']['error']['code'], 'CALL_INTERRUPTED')
        self.assertEqual(restored.get(session['id'])['usage']['elapsed_seconds'], 30)
        self.assertEqual(restored.get(session['id'])['status'], 'exhausted')

    def test_concurrent_call_cannot_double_spend(self):
        session = self.session(max_calls=1)
        entered, release = threading.Event(), threading.Event()
        original = self.tools._dispatch
        def paused(*args):
            entered.set(); self.assertTrue(release.wait(5)); return original(*args)
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.tools, '_dispatch', side_effect=paused):
            future = pool.submit(self.tools.call, session['id'], 'read_case', {}, 'same')
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises(ToolServiceError) as raised:
                    ResearchTools(self.store).call(session['id'], 'read_case', {}, 'same')
                self.assertEqual(raised.exception.code, 'SESSION_BUSY'); self.assertTrue(raised.exception.retryable)
            finally:
                release.set()
            first = future.result(timeout=5)
        self.assertEqual(self.tools.call(session['id'], 'read_case', {}, 'same'), first)
        self.assertEqual(self.tools.get(session['id'])['usage']['calls'], 1)

    def test_source_tampering_closes_session_without_returning_results(self):
        session = self.session()
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE author_studies SET created_at=? WHERE id=?', ('tampered', self.study['id']))
        failed = self.tools.call(session['id'], 'read_result', {}, 'read')
        self.assertEqual(failed['response']['error']['code'], 'SOURCE_INTEGRITY')
        self.assertFalse(failed['response']['error']['retryable']); self.assertIsNone(failed['response']['result_json'])
        self.assertEqual(self.tools.get(session['id'])['status'], 'blocked')
        with self.assertRaises(ToolServiceError) as raised:
            self.tools.call(session['id'], 'read_case', {}, 'next')
        self.assertEqual(raised.exception.code, 'SESSION_BLOCKED')

    def test_failed_call_is_retained_and_transient_retry_is_bounded(self):
        session = self.session()
        with patch.object(self.tools, '_case', side_effect=sqlite3.OperationalError('locked')):
            first = self.tools.call(session['id'], 'read_case', {}, 'first')
        self.assertEqual(first['response']['error']['code'], 'STORE_BUSY')
        self.assertTrue(first['response']['error']['retryable'])
        self.assertEqual(self.tools.call(session['id'], 'read_case', {}, 'first'), first)
        second = self.tools.call(session['id'], 'read_case', {}, 'retry')
        self.assertTrue(second['response']['ok']); self.assertEqual(second['sequence'], 2)
        self.assertEqual(self.tools.get(session['id'])['usage']['errors'], 1)

    def test_ledger_payload_tampering_is_not_replayed(self):
        session = self.session()
        call = self.tools.call(session['id'], 'read_result', {}, 'first')
        with transaction(self.store.db_path) as connection:
            connection.execute('UPDATE research_tool_calls SET payload=? WHERE id=?', ('{}', call['id']))
        with self.assertRaises(ToolServiceError) as raised:
            self.tools.call(session['id'], 'read_result', {}, 'first')
        self.assertEqual(raised.exception.code, 'LEDGER_INTEGRITY')

    def test_deleted_tail_does_not_restore_budget(self):
        session = self.session(max_calls=1)
        self.tools.call(session['id'], 'read_result', {}, 'first')
        with transaction(self.store.db_path) as connection:
            connection.execute('DELETE FROM research_tool_calls WHERE session_id=?', (session['id'],))
        for operation in (lambda: self.tools.get(session['id']),
                          lambda: self.tools.call(session['id'], 'read_case', {}, 'next')):
            with self.assertRaises(ToolServiceError) as raised:
                operation()
            self.assertEqual(raised.exception.code, 'LEDGER_INTEGRITY')

    def test_filter_paging_and_input_boundaries(self):
        session = self.session()
        self.assertEqual(self.tools.list(case_id=self.case['id'])['items'], [{key: value for key, value in session.items() if key != 'calls'}])
        self.assertEqual(self.tools.list(case_id='research_case_' + '0' * 64)['total'], 0)
        for args in ({'value': float('nan')}, {'value': '\ud800'}, {1: 'bad'}, {'value': object()}):
            with self.subTest(args=repr(args)), self.assertRaises(ToolServiceError):
                self.tools.call(session['id'], 'read_case', args, 'bad')
        for limit in (True, 0, 101):
            with self.assertRaises(ToolServiceError):
                self.tools.list(limit=limit)
        self.assertEqual(self.tools.get(session['id'])['usage']['calls'], 0)

    def test_http_argument_model_is_closed_and_null_defaults_are_normalized(self):
        session = self.session()
        request = ToolCallCreate.model_validate({'tool': 'read_case', 'arguments': {}, 'idempotency_key': 'read'}).model_dump()
        first = self.tools.call(session['id'], **request)
        self.assertEqual(self.tools.call(session['id'], 'read_case', {}, 'read'), first)
        with self.assertRaises(ValueError):
            ToolCallCreate.model_validate({'tool': 'propose_next_action', 'arguments': self.proposal(metrics=2), 'idempotency_key': 'bad'})

    def test_cumulative_utf8_output_budget_discards_oversize_result(self):
        session = self.session(max_calls=1)
        original = self.tools._dispatch
        def large(*args):
            value = original(*args)
            value['result_json'] = '量' * 600
            return value
        with patch.object(self.tools, '_dispatch', side_effect=large), patch('paper_alpha.server.research_tools.MAX_SESSION_BYTES', 2000):
            call = self.tools.call(session['id'], 'read_result', {}, 'large')
            self.assertEqual(call['response']['error']['code'], 'RESPONSE_TOO_LARGE')
            self.assertIsNone(call['response']['result_json'])
            self.assertEqual(self.tools.get(session['id'])['status'], 'blocked')
        with self.assertRaises(ToolServiceError) as raised:
            self.tools.call(session['id'], 'read_case', {}, 'next')
        self.assertEqual(raised.exception.code, 'SESSION_BLOCKED')


if __name__ == '__main__':
    unittest.main()
