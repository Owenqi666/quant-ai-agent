"""Authority-gated AV originals to a managed development-only OHLC prefix.

The bounded byte scanner decodes only metadata, dates and development OHLC
strings. Final value tokens and every volume value are skipped without decoding.
"""
from __future__ import annotations

from contextlib import closing
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import uuid

import pandas as pd

from .storage import digest, json_text
from .real_daily_admission import (RealDailyAdmissionError, need, _closed, _path, _read,
    _immutable, _now, _utc, _json, _selection, _verify_history, code_identity, MAX_RAW)

VERSION = 'real-daily-ohlc-prefix-normalizer-v1'
FIELDS = ('open', 'high', 'low', 'close')
BAR_KEYS = {'1. open': 'open', '2. high': 'high', '3. low': 'low', '4. close': 'close', '5. volume': 'volume'}
PRICE = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z')
NUMBER = re.compile(rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')


class _Scanner:
    """Lexical scanner; skipped values are not materialized as Python objects."""
    def __init__(self, payload):
        need(type(payload) is bytes and 0 < len(payload) <= MAX_RAW, 'bounded_provider_bytes_required')
        self.b = payload; self.i = 0; self.tokens = 0
        self.price_decodes = 0; self.final_decodes = 0; self.volume_decodes = 0

    def ws(self):
        while self.i < len(self.b) and self.b[self.i] in b' \t\r\n': self.i += 1

    def peek(self):
        self.ws(); return self.b[self.i:self.i + 1]

    def char(self, wanted):
        self.ws(); need(self.b[self.i:self.i + 1] == wanted, 'invalid_provider_json')
        self.i += 1; self.tokens += 1
        need(self.tokens <= 200000, 'provider_complexity_limit')

    def string_token(self):
        self.ws(); need(self.b[self.i:self.i + 1] == b'"', 'provider_string_required')
        start = self.i; self.i += 1
        while self.i < len(self.b):
            c = self.b[self.i]; self.i += 1
            if c == 34: return self.b[start:self.i]
            need(c >= 32, 'invalid_provider_json')
            if c == 92:
                need(self.i < len(self.b), 'invalid_provider_json')
                escape = self.b[self.i]; self.i += 1
                need(escape in b'"\\/bfnrtu', 'invalid_provider_json')
                if escape == 117:
                    need(len(self.b[self.i:self.i + 4]) == 4 and all(c in b'0123456789abcdefABCDEF' for c in self.b[self.i:self.i + 4]), 'invalid_provider_json')
                    self.i += 4
        raise RealDailyAdmissionError('invalid_provider_json')

    def string(self, *, price=False):
        token = self.string_token()
        try: value = json.loads(token.decode('utf-8'))
        except (ValueError, UnicodeError): raise RealDailyAdmissionError('invalid_provider_string') from None
        need(type(value) is str and len(value) <= (64 if price else 1000), 'bounded_provider_string_required')
        if price: self.price_decodes += 1
        return value

    def skip(self, depth=0):
        need(depth <= 16, 'provider_complexity_limit')
        self.ws(); c = self.peek()
        if c == b'"': self.string_token(); return
        if c in (b'{', b'['):
            opener, closer = (b'{', b'}') if c == b'{' else (b'[', b']')
            self.char(opener)
            if self.peek() == closer: self.char(closer); return
            while True:
                if opener == b'{': self.string_token(); self.char(b':')
                self.skip(depth + 1)
                if self.peek() == closer: self.char(closer); return
                self.char(b',')
        for literal in (b'true', b'false', b'null'):
            if self.b[self.i:self.i + len(literal)] == literal:
                self.i += len(literal); return
        match = NUMBER.match(self.b, self.i)
        need(match is not None, 'invalid_provider_json')
        self.i = match.end()  # no int/float conversion for skipped numeric tokens

    def metadata(self):
        self.char(b'{'); result = {}
        if self.peek() == b'}': self.char(b'}'); return result
        while True:
            key = self.string(); need(key not in result, 'duplicate_provider_key'); self.char(b':')
            result[key] = self.string()
            if self.peek() == b'}': self.char(b'}'); return result
            self.char(b',')

    def bar(self):
        self.char(b'{'); row = {}; seen = set()
        while True:
            key = self.string(); need(key in BAR_KEYS and key not in seen, 'invalid_development_bar_shape')
            seen.add(key); self.char(b':')
            field = BAR_KEYS[key]
            if field == 'volume': self.skip()
            else:
                text = self.string(price=True)
                need(PRICE.fullmatch(text) is not None, 'invalid_development_price')
                number = float(text)
                need(math.isfinite(number) and number > 0, 'invalid_development_price')
                row[field] = number
            if self.peek() == b'}': self.char(b'}'); break
            self.char(b',')
        need(seen == set(BAR_KEYS), 'invalid_development_bar_shape')
        need(row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high'], 'invalid_ohlc_bounds')
        return row

    def series(self, selection):
        self.char(b'{'); rows = {}; dates = set(); dev = set(selection['development_sessions'])
        if self.peek() == b'}': self.char(b'}'); return rows, dates
        while True:
            day = self.string()
            need(re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) is not None and day not in dates, 'invalid_provider_date_key')
            dates.add(day); need(len(dates) <= 1024, 'provider_record_limit'); self.char(b':')
            if day in dev: rows[day] = self.bar()
            else: self.skip()
            if self.peek() == b'}': self.char(b'}'); return rows, dates
            self.char(b',')


def decode_development_bytes(payload, *, asset, selection):
    """Pure scanner for testing. Production calls it only after committed scope."""
    selection = _selection(selection); need(asset in selection['assets'], 'invalid_asset')
    scanner = _Scanner(payload); scanner.char(b'{'); seen = set(); meta = rows = dates = None
    while True:
        key = scanner.string(); need(key not in seen and key in ('Meta Data', 'Time Series (Daily)'), 'invalid_provider_shape')
        seen.add(key); scanner.char(b':')
        if key == 'Meta Data': meta = scanner.metadata()
        else: rows, dates = scanner.series(selection)
        if scanner.peek() == b'}': scanner.char(b'}'); break
        scanner.char(b',')
    scanner.ws(); need(scanner.i == len(payload) and seen == {'Meta Data', 'Time Series (Daily)'}, 'invalid_provider_shape')
    need(type(meta) is dict and meta.get('2. Symbol') == asset, 'provider_asset_mismatch')
    need(dates == set(selection['sessions']) and set(rows) == set(selection['development_sessions']), 'provider_date_grid_mismatch')
    need(scanner.price_decodes == 4 * len(selection['development_sessions']), 'numeric_scope_mismatch')
    return rows, {'development_price_values_decoded': scanner.price_decodes,
                  'final_value_tokens_decoded': 0, 'final_numeric_rows_parsed': 0,
                  'volume_value_tokens_decoded': 0, 'full_dataset_validated': False,
                  'whole_original_bytes_scanned': len(payload)}


def _out(host, out_dir):
    out = _path(out_dir)
    need(out.parent == host.root / 'real_daily_prefixes' and out.name not in ('', '.', '..'), 'managed_prefix_output_required')
    need(not out.exists(), 'output_already_exists')
    return out


def normalize_development(host, approval_id, approval_digest, out_dir, *, idempotency_key,
                          allow_whole_byte_integrity=False):
    """A committed reservation precedes raw open; no implicit numeric retry."""
    need(type(allow_whole_byte_integrity) is bool and allow_whole_byte_integrity, 'whole_byte_integrity_permission_required')
    out = _path(out_dir)
    authority = host.assert_approved(approval_id, approval_digest)
    reservation = host.reserve(approval_id, approval_digest, idempotency_key=idempotency_key)
    if reservation['status'] == 'registered':
        existing = host.resolve_registration(reservation['registration_id'], host.get_registration(reservation['registration_id'])['registration_digest'])
        need(Path(existing['prefix_path']).parent == out, 'idempotency_output_conflict')
        return host.get_registration(reservation['registration_id'])
    need(reservation['status'] == 'reserved', 'normalization_not_retriable')
    out = _out(host, out)
    selection = authority['assessment']['selection']
    owner, attempt_id = str(uuid.uuid4()), str(uuid.uuid4())
    with host._transaction() as c:
        _verify_history(c)
        current = c.execute('SELECT * FROM rdl_reservations WHERE id=?', (reservation['id'],)).fetchone()
        need(current is not None and current['status'] == 'reserved', 'normalization_owner_conflict')
        need(c.execute('SELECT 1 FROM rdl_normalize_attempts WHERE reservation_id=?', (reservation['id'],)).fetchone() is None, 'normalization_not_retriable')
        c.execute("UPDATE rdl_reservations SET status='normalizing',owner_token=? WHERE id=?", (owner, reservation['id']))
        c.execute('INSERT INTO rdl_normalize_attempts VALUES (?,?,?,?,?,?,?)', (attempt_id, reservation['id'], owner, 'running', _now(), None, None))
    # Reservation and ownership claim are durable before any market original IO.
    try:
        out.parent.mkdir(parents=True, exist_ok=True); out.mkdir()
        authority = host.assert_approved(approval_id, approval_digest)
        rows_by_asset = {}; raw_receipts = []; counts = []
        for item in authority['assessment']['input_pins']['raw']:
            host.assert_approved(approval_id, approval_digest)
            raw = _read(item['path'], MAX_RAW)
            need(len(raw) == item['size_bytes'] and hashlib.sha256(raw).hexdigest() == item['sha256'], 'raw_identity_mismatch')
            rows, scope = decode_development_bytes(raw, asset=item['asset'], selection=selection)
            rows_by_asset[item['asset']] = rows; counts.append(scope)
            raw_receipts.append({'asset': item['asset'], 'sha256': item['sha256'], 'size_bytes': len(raw)})
        host.assert_approved(approval_id, approval_digest)
        stream = io.StringIO(newline=''); writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(['date', 'asset', *FIELDS]); observations = []
        for day in selection['development_sessions']:
            for asset in selection['assets']:
                values = [rows_by_asset[asset][day][f] for f in FIELDS]
                writer.writerow([day, asset, *[format(v, '.17g') for v in values]])
                observations.append({'date': day, 'asset': asset, 'values_hex': [v.hex() for v in values]})
        prefix = stream.getvalue().encode(); prefix_path = out / 'prefix.csv'
        prefix_sha = hashlib.sha256(prefix).hexdigest()
        receipt = {'schema_version': 1, 'normalizer_version': VERSION,
                   'reservation_id': reservation['id'], 'reservation_digest': reservation['reservation_digest'],
                   'assessment_digest': authority['assessment_digest'], 'approval_digest': approval_digest,
                   'selection_digest': digest(selection), 'method_digest': authority['assessment']['method_digest'],
                   'prefix_sha256': prefix_sha, 'development_content_digest': digest(observations),
                   'development_row_count': len(observations), 'raw_identities': raw_receipts,
                   'raw_originals_opened': len(raw_receipts), 'raw_originals_whole_byte_hashed': len(raw_receipts),
                   'development_price_values_decoded': sum(s['development_price_values_decoded'] for s in counts),
                   'final_value_tokens_decoded': 0, 'final_numeric_rows_parsed': 0,
                   'volume_value_tokens_decoded': 0, 'full_dataset_validated': False,
                   'source_authenticated': False, 'real_source_ready': False, 'test_freshness': 'unknown',
                   'code_identity': code_identity()}
        receipt_bytes = json_text(receipt).encode(); receipt_path = out / 'receipt.json'
        identity = str(uuid.uuid4())
        reg = {'schema_version': 1, 'registration_version': VERSION, 'id': identity,
               'approval_id': approval_id, 'approval_digest': approval_digest,
               'assessment_id': authority['assessment_id'], 'assessment_digest': authority['assessment_digest'],
               'reservation_id': reservation['id'], 'reservation_digest': reservation['reservation_digest'],
               'prefix_path': str(prefix_path), 'prefix_sha256': prefix_sha,
               'receipt_path': str(receipt_path), 'receipt_sha256': hashlib.sha256(receipt_bytes).hexdigest(),
               'selection_json': json_text(selection), 'method_digest': authority['assessment']['method_digest'],
               'code_identity_json': json_text(code_identity()), 'origin': authority['assessment']['origin'],
               'status': 'registered', 'final_numeric_access_allowed': False,
               'source_authenticated': False, 'real_source_ready': False}
        pin = digest(reg); published = {**reg, 'registration_digest': pin}
        _immutable(prefix_path, prefix); _immutable(receipt_path, receipt_bytes)
        _immutable(out / 'registration.json', json_text(published).encode())
        host.assert_approved(approval_id, approval_digest)
        with host._transaction() as c:
            _verify_history(c)
            current = c.execute('SELECT * FROM rdl_reservations WHERE id=?', (reservation['id'],)).fetchone()
            need(current['owner_token'] == owner and current['status'] == 'normalizing', 'normalization_owner_lost')
            need(c.execute('SELECT 1 FROM rdl_revocations WHERE approval_id=?', (approval_id,)).fetchone() is None, 'approval_revoked')
            need(_utc(authority['expires_at_utc']) > _utc(_now()), 'approval_expired')
            host._assert_inputs(authority['assessment'])
            need(hashlib.sha256(_read(prefix_path)).hexdigest() == prefix_sha and hashlib.sha256(_read(receipt_path)).hexdigest() == reg['receipt_sha256'], 'prefix_pin_drift')
            c.execute('INSERT INTO rdl_registrations VALUES (?,?,?,?)', (identity, reservation['id'], pin, json_text(reg)))
            c.execute("UPDATE rdl_reservations SET status='registered',registration_id=? WHERE id=?", (identity, reservation['id']))
            c.execute("UPDATE rdl_normalize_attempts SET status='completed',finished_at=? WHERE id=? AND owner_token=?", (_now(), attempt_id, owner))
        host.resolve_registration(identity, pin)
        return published
    except Exception as exc:
        code = getattr(exc, 'code', 'normalization_failure')
        with host._transaction() as c:
            c.execute("UPDATE rdl_reservations SET status='failed',error_code=? WHERE id=? AND owner_token=? AND status='normalizing'", (code, reservation['id'], owner))
            c.execute("UPDATE rdl_normalize_attempts SET status='failed',error_code=?,finished_at=? WHERE id=? AND owner_token=? AND status='running'", (code, _now(), attempt_id, owner))
        if out.is_dir() and not (out / 'failure.json').exists():
            _immutable(out / 'failure.json', json_text({'schema_version': 1, 'reservation_id': reservation['id'], 'attempt_id': attempt_id, 'error_code': code, 'automatic_retry_allowed': False}).encode())
        if isinstance(exc, RealDailyAdmissionError): raise
        raise RealDailyAdmissionError(code) from None


def load_registered_panels(host, registration_id, expected_digest):
    """Only managed prefix numbers; never open the provider originals."""
    reg = host.resolve_registration(registration_id, expected_digest)
    selection = reg['selection']
    raw = _read(reg['prefix_path'])
    need(hashlib.sha256(raw).hexdigest() == reg['prefix_sha256'], 'prefix_pin_drift')
    try:
        reader = csv.reader(io.StringIO(raw.decode('utf-8'), newline=''))
        need(next(reader) == ['date', 'asset', *FIELDS], 'prefix_columns_mismatch')
        rows = list(reader)
        expected = [(d, a) for d in selection['development_sessions'] for a in selection['assets']]
        need(len(rows) == len(expected), 'prefix_grid_mismatch')
        values = {f: [] for f in FIELDS}; observations = []
        for row, (day, asset) in zip(rows, expected):
            need(len(row) == 6 and row[:2] == [day, asset], 'prefix_grid_mismatch')
            numbers = [float(v) for v in row[2:]]
            need(all(math.isfinite(v) and v > 0 for v in numbers), 'invalid_prefix_numeric')
            o, h, l, c = numbers
            need(l <= min(o, c) <= max(o, c) <= h, 'invalid_ohlc_bounds')
            for f, v in zip(FIELDS, numbers): values[f].append(v)
            observations.append({'date': day, 'asset': asset, 'values_hex': [v.hex() for v in numbers]})
        receipt = _json(_read(reg['receipt_path']))
        need(receipt['development_content_digest'] == digest(observations) and receipt['development_row_count'] == len(expected), 'prefix_content_drift')
        panels = {f: pd.DataFrame([values[f][i:i + len(selection['assets'])] for i in range(0, len(expected), len(selection['assets']))],
                  index=pd.to_datetime(selection['development_sessions']), columns=selection['assets']) for f in FIELDS}
    except (ValueError, UnicodeError, StopIteration, OverflowError):
        raise RealDailyAdmissionError('invalid_registered_prefix') from None
    host.resolve_registration(registration_id, expected_digest)
    return panels
