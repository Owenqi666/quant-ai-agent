"""Persisted event cursors survive large histories, gaps and new attempts."""
import unittest

from paper_alpha.server.db import transaction
from tests import test_server


class EventPages(unittest.TestCase):
    setUp = test_server.ServerCase.setUp
    tearDown = test_server.ServerCase.tearDown
    submit = test_server.ServerCase.submit

    def add(self, run_id, count, attempt='attempt-1'):
        with transaction(self.store.db_path) as connection:
            for i in range(count):
                self.store._event(connection, run_id, 'fixture', {'engine_sequence': i + 1}, attempt)

    def test_complete_pages_for_boundaries(self):
        for count in (0, 999, 1000, 1001, 2501):
            run = self.submit(str(count))
            with transaction(self.store.db_path) as connection:
                connection.execute('DELETE FROM events WHERE run_id=?', (run['id'],))
            self.add(run['id'], count)
            page = self.store.event_page(run['id'])
            watermark, ids = page['high_watermark'], []
            while True:
                ids.extend(e['sequence'] for e in page['items'])
                self.assertEqual(page['total_records'], count)
                if not page['has_more']:
                    break
                page = self.store.event_page(run['id'], page['next_cursor'], through=watermark)
            self.assertEqual(len(ids), count)
            self.assertEqual(ids, sorted(set(ids)))

    def test_snapshot_tail_gaps_attempts_and_legacy(self):
        run, other = self.submit('first'), self.submit('other')
        self.add(run['id'], 1001)
        first = self.store.event_page(run['id'], limit=1000)
        self.add(other['id'], 10)
        self.add(run['id'], 20, 'attempt-2')
        tail = self.store.event_page(run['id'], first['next_cursor'], through=first['high_watermark'])
        self.assertEqual(len(tail['items']), 2)
        self.assertFalse(tail['has_more'])
        fresh = self.store.event_page(run['id'], tail['next_cursor'])
        self.assertEqual(len(fresh['items']), 20)
        self.assertEqual(fresh['items'][0]['payload']['engine_sequence'], 1)
        self.assertEqual(fresh['items'][0]['attempt_id'], 'attempt-2')
        self.assertEqual(fresh['total_records'], 1022)
        self.assertEqual(len(self.client.get('/api/runs/' + run['id'] + '/events').json()), 1000)
        current = self.client.get('/api/runs/' + run['id'] + '/events/page', params={'after': tail['next_cursor']})
        self.assertEqual(current.json()['items'], fresh['items'])

    def test_invalid_cursor_and_limits(self):
        run = self.submit()
        url = '/api/runs/' + run['id'] + '/events/page'
        for params in ({'after': -1}, {'limit': 0}, {'limit': 1001}, {'through': -1}):
            self.assertEqual(self.client.get(url, params=params).status_code, 422)
        for params in ({'after': 100000}, {'through': 100000}):
            self.assertEqual(self.client.get(url, params=params).status_code, 409)
        self.assertEqual(self.client.get('/api/runs/missing/events/page').status_code, 404)
