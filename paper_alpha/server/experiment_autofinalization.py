"""Bounded bookkeeping for already submitted experiments; no AI or new runs."""
from __future__ import annotations

from contextlib import closing
from collections import OrderedDict
import logging
import time

from ..storage import digest
from .db import connect
from .research_jobs import ResearchJobs
from .research_session_experiments import SessionExperiments, _exists

LOG = logging.getLogger('paper_alpha.experiment_finalizer')


class ExperimentAutofinalizer:
    """Workspace-local rotating sweep. The worker owns scheduling and its lock."""
    def __init__(self, store, *, batch_size=20, min_interval_seconds=1.0, max_tick_seconds=2.0):
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError('Finalization batch size must be 1..100')
        if (isinstance(min_interval_seconds, bool) or not isinstance(min_interval_seconds, (int, float))
                or not 0 <= min_interval_seconds <= 60):
            raise ValueError('Finalization interval must be 0..60 seconds')
        if (isinstance(max_tick_seconds, bool) or not isinstance(max_tick_seconds, (int, float))
                or not 0 < max_tick_seconds <= 30):
            raise ValueError('Finalization round must be bounded to 0..30 seconds')
        self.max_tick_seconds = float(max_tick_seconds)
        self.store = store
        self.batch_size = batch_size
        self.min_interval_seconds = float(min_interval_seconds)
        self._cursor = None
        self._last_tick = None
        self._error_log = OrderedDict()
        self.experiments = SessionExperiments(store)
        self.jobs = ResearchJobs(store)

    def _rows(self):
        with closing(connect(self.store.db_path)) as connection:
            if not _exists(connection):
                return []
            # Invalid JSON must enter normal integrity handling, rather than
            # crashing the whole selection or disappearing from its denominator.
            condition = "json_extract(CASE WHEN json_valid(r.payload) THEN r.payload ELSE '{}' END,'$.status') IS NOT 'finished'"
            query = ('SELECT e.id,e.created_at FROM research_session_experiments e '
                     'LEFT JOIN research_session_reservations r ON r.token=e.reservation_token WHERE ' + condition)
            args = ()
            if self._cursor is not None:
                query += ' AND (e.created_at,e.id)>(?,?)'
                args = self._cursor
            rows = connection.execute(query + ' ORDER BY e.created_at,e.id LIMIT ?', (*args, self.batch_size)).fetchall()
            if not rows and self._cursor is not None:
                self._cursor = None
                query = ('SELECT e.id,e.created_at FROM research_session_experiments e '
                         'LEFT JOIN research_session_reservations r ON r.token=e.reservation_token WHERE ' + condition)
                rows = connection.execute(query + ' ORDER BY e.created_at,e.id LIMIT ?', (self.batch_size,)).fetchall()
            return rows

    def _log_pause(self, identity, code, stamp):
        old = self._error_log.get(identity)
        if old is None or old[0] != code or stamp - old[1] >= 30:
            LOG.warning('experiment_finalization_paused experiment=%s code=%s', identity, code)
            self._error_log[identity] = (code, stamp)
            self._error_log.move_to_end(identity)
            if len(self._error_log) > 256:
                self._error_log.popitem(last=False)

    def tick(self, *, force=False):
        result = {'scanned': 0, 'changed': 0, 'completed': 0, 'stopped': 0, 'errors': [], 'throttled': False}
        stamp = time.monotonic()
        if not force and self._last_tick is not None and stamp - self._last_tick < self.min_interval_seconds:
            result['throttled'] = True
            return result
        self._last_tick = stamp
        try:
            rows = self._rows()
        except Exception as exc:
            result['errors'].append({'experiment_id': None, 'code': getattr(exc, 'code', 'SCAN_FAILED')})
            LOG.exception('experiment_finalization_scan_failed code=%s', getattr(exc, 'code', 'SCAN_FAILED'))
            return result
        for row in rows:
            if result['scanned'] and time.monotonic() - stamp >= self.max_tick_seconds:
                break
            self._cursor = (row['created_at'], row['id'])
            result['scanned'] += 1
            try:
                before = self.experiments.get(row['id'])
                if before['reservation']['outcome'] is not None:
                    self._error_log.pop(row['id'], None)
                    continue
                if before['job'] is not None:
                    self.jobs.finalize_existing_run(before['job']['id'], row['id'])
                # Reconcile existing terminal children, including projected job
                # budget exhaustion. This never ensures/creates a missing job.
                after = self.experiments.reconcile(row['id'])
                old = digest({'reservation': before['reservation'], 'steps': before['job']['steps'] if before['job'] else None})
                new = digest({'reservation': after['reservation'], 'steps': after['job']['steps'] if after['job'] else None})
                if old != new:
                    self._error_log.pop(row['id'], None)
                    result['changed'] += 1
                    outcome = after['reservation']['outcome']
                    if outcome is not None:
                        result['completed' if outcome['status'] == 'completed' else 'stopped'] += 1
                    LOG.info('experiment_finalized experiment=%s job=%s state=%s outcome=%s',
                             row['id'], after['job']['id'] if after['job'] else None,
                             after['job']['state'] if after['job'] else None,
                             outcome['code'] if outcome else None)
            except Exception as exc:
                # Corrupt/source-stale/busy candidates cannot stop unrelated
                # computation or force a fresh action key. Never log source text.
                code = getattr(exc, 'code', type(exc).__name__)
                result['errors'].append({'experiment_id': row['id'], 'code': code})
                self._log_pause(row['id'], code, stamp)
        return result
