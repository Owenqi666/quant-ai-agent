"""Independent small HDF fixtures, never evidence about actual author returns."""
from contextlib import contextmanager
from copy import deepcopy
from fractions import Fraction
import hashlib
import math
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np

from paper_alpha import author_archive_contract as source_contract
from paper_alpha import eligibility_archive as scanner
from paper_alpha.storage import digest


def make_fixture(directory, filename='USData.mat', assets=12):
    """A deliberately synthetic 18-month source admitted only by a test patch."""
    international = filename == 'IntnlData.mat'
    if international:
        assets = 49
    path = Path(directory) / filename
    stamps = [199201 + i for i in range(12)] + [199301 + i for i in range(6)]
    with h5py.File(path, 'w') as f:
        f['N'], f['T'] = np.array([[assets]], dtype=np.float64), np.array([[18]], dtype=np.float64)
        f['ym' if international else 'ymd'] = np.array([stamps if international else [m * 100 + 28 for m in stamps]], dtype=np.float64)
        for field, value in [('Return', .01), ('DGW', .2), ('MV', 100.)]:
            dtype = np.float64 if international and field != 'DGW' else np.float32
            f.create_dataset(field, data=np.full((18, assets), value, dtype=dtype), chunks=(18, 2), compression='gzip')
        if international:
            f['NumAll'] = np.ones((1, 49), dtype=np.float64)
            codes = [chr(65 + i // 26) + chr(65 + i % 26) for i in range(49)]
            f['CountryCodes'] = np.array([[ord(code[j]) for code in codes] for j in range(2)], dtype=np.uint16)
    return path


@contextmanager
def pinned_fixture(path):
    body = path.read_bytes()
    source = source_contract.source_metadata(path.name)
    with h5py.File(path, 'r') as archive:
        source.update(assets=int(archive['N'][0, 0]), periods=int(archive['T'][0, 0]))
    source.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                  repository_md5=hashlib.md5(body).hexdigest(), first_month='1992-01', last_month='1993-06')
    with patch.dict(source_contract.SOURCES, {path.name: source}):
        yield source


def plan_fixture(filename='USData.mat', end='1993-04'):
    return {'schema_version': 1, 'source_file': filename,
            'development_start': '1993-01', 'development_end': end, 'reserved_from': '1993-05',
            'task': 'momentum', 'requires_market_cap': False, 'minimum_assets': 1,
            'threshold_origin': 'project_screen', 'rationale': 'Synthetic fixture engineering check only.'}


def scalar_reference(path, plan):
    """Independent scalar indexing and exact products; no scanner math helpers."""
    output = []
    def ordinal(month):
        year, number = (int(piece) for piece in month.split('-'))
        return year * 12 + number - 1
    with h5py.File(path, 'r') as archive:
        count = int(archive['N'][0, 0])
        start = ordinal('1992-01')
        for month in range(ordinal(plan['development_start']), ordinal(plan['development_end']) + 1):
            current = month - start
            entry = {'month': f'{month // 12:04d}-{month % 12 + 1:02d}', 'patterns': [0] * 8,
                     'momentum_missing': 0, 'momentum_invalid': 0, 'momentum_unrepresentable': 0}
            for row in range(count):
                history = [float(archive['Return'][current - backward, row]) for backward in range(12, 1, -1)]
                ready = False
                if any(not math.isfinite(value) for value in history):
                    entry['momentum_missing'] += 1
                elif any(value < -1 for value in history):
                    entry['momentum_invalid'] += 1
                else:
                    total = Fraction(1)
                    for value in history:
                        total *= 1 + Fraction.from_float(value)
                    try:
                        ready = math.isfinite(float(total - 1))
                    except OverflowError:
                        ready = False
                    if not ready:
                        entry['momentum_unrepresentable'] += 1
                dg, mv = float(archive['DGW'][current - 1, row]), float(archive['MV'][current - 1, row])
                pattern = int(ready) + 2 * int(math.isfinite(dg) and -1 <= dg <= 1) + 4 * int(math.isfinite(mv) and mv > 0)
                entry['patterns'][pattern] += 1
            output.append(entry)
    return output


class EligibilityArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_mixed_patterns_missing_precedence_and_exact_fraction_reference(self):
        path = make_fixture(self.directory)
        with h5py.File(path, 'r+') as f:
            f['Return'][2, 1] = np.nan
            f['Return'][3, 1] = -2  # Missing takes precedence over invalid.
            f['Return'][2, 2] = -2
            f['Return'][:11, 3] = 1e38  # finite float32 values, overflowing compound
            f['Return'][:11, 4] = 1e38
            f['Return'][8, 4] = -1  # exact zero factor overrides overflow
            f['Return'][:11, 5] = 0
            f['DGW'][11, 6] = np.nan
            f['MV'][11, 7] = 0
            f['DGW'][11, 8], f['MV'][11, 8] = -2, -1
            f['Return'][4, 9] = np.inf
        plan = plan_fixture()
        expected = scalar_reference(path, plan)
        with pinned_fixture(path):
            actual = scanner.scan(path, plan)
        self.assertEqual(actual['months'], expected)
        self.assertEqual(actual['plan_digest'], digest(plan))
        self.assertTrue(all(sum(item['patterns']) == 12 for item in actual['months']))
        first = actual['months'][0]
        self.assertEqual((first['momentum_missing'], first['momentum_invalid'], first['momentum_unrepresentable']), (2, 1, 1))

    def test_original_rows_not_dropped_and_block_size_does_not_change_scan(self):
        path = make_fixture(self.directory, 'IntnlData.mat')
        plan = plan_fixture(path.name)
        with pinned_fixture(path):
            with patch.object(scanner, 'ROW_BLOCK', 2):
                small = scanner.scan(path, plan)
            with patch.object(scanner, 'ROW_BLOCK', 7):
                uneven = scanner.scan(path, plan)
        self.assertEqual(small, uneven)
        self.assertEqual(small['months'], scalar_reference(path, plan))
        self.assertEqual([sum(row['patterns']) for row in small['months']], [49] * 4)

    def test_rounding_near_float_maximum_does_not_hide_true_overflow(self):
        path = make_fixture(self.directory, 'IntnlData.mat')
        plan = plan_fixture(path.name, end='1993-01')
        with h5py.File(path, 'r+') as f:
            f['Return'][:11, :2] = 0
            f['Return'][0, :2] = sys.float_info.max
            f['Return'][1, 1] = 1e-15
        with pinned_fixture(path):
            observed = scanner.scan(path, plan)
        self.assertEqual(observed['months'], scalar_reference(path, plan))
        self.assertEqual(observed['months'][0]['momentum_unrepresentable'], 1)

    def test_target_labels_do_not_change_own_or_prior_qualification(self):
        path = make_fixture(self.directory)
        plan = plan_fixture()
        with pinned_fixture(path):
            first = scanner.scan(path, plan)
        with h5py.File(path, 'r+') as f:
            f['Return'][12, :] = np.nan  # January target; history for March and April.
            f['Return'][15:, :] = np.nan  # April onward: never read by this plan.
        with pinned_fixture(path):
            changed = scanner.scan(path, plan)
        self.assertEqual(first['months'][:2], changed['months'][:2])
        self.assertNotEqual(first['months'][2:], changed['months'][2:])
        self.assertEqual(changed['months'], scalar_reference(path, plan))

    def test_one_verification_and_one_bounded_read_per_field_per_block(self):
        from paper_alpha import author_archive
        path = make_fixture(self.directory)
        observed = []
        original = h5py.Dataset.__getitem__
        def record(dataset, selection):
            if dataset.name in ('/Return', '/DGW', '/MV'):
                observed.append((dataset.name, selection))
            return original(dataset, selection)
        with pinned_fixture(path), patch.object(scanner, 'ROW_BLOCK', 4), \
                patch.object(author_archive, '_verified_archive', wraps=author_archive._verified_archive) as verified, \
                patch.object(h5py.Dataset, '__getitem__', record):
            scanner.scan(path, plan_fixture())
        self.assertEqual(verified.call_count, 1)
        self.assertEqual(len(observed), 9)  # Three fields x three original-row blocks.
        for name, (time, rows) in observed:
            self.assertLessEqual(rows.stop - rows.start, 4)
            self.assertEqual((time.start, time.stop), (0, 14) if name == '/Return' else (11, 15))

    def test_digest_bad_plan_wrong_source_and_mutation_fail_closed(self):
        path = make_fixture(self.directory)
        with self.assertRaises(ValueError):
            scanner.scan(path, plan_fixture())
        with pinned_fixture(path):
            invalid = plan_fixture(); invalid['development_end'] = '1993-05'
            with self.assertRaises(ValueError):
                scanner.scan(path, invalid)
            with self.assertRaisesRegex(ValueError, 'filename'):
                scanner.scan(path, plan_fixture('IntnlData.mat'))
            original = scanner._representable
            touched = False
            def touch(values):
                nonlocal touched
                if not touched:
                    info = path.stat()
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
                    touched = True
                return original(values)
            with patch.object(scanner, '_representable', side_effect=touch), self.assertRaisesRegex(ValueError, 'changed'):
                scanner.scan(path, plan_fixture())


if __name__ == '__main__':
    unittest.main()
