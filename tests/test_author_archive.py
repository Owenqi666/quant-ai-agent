"""Tiny HDF engineering fixtures, explicitly not copies of the author data."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np

from paper_alpha import author_archive as reader, author_archive_contract as contract
from paper_alpha.author_panel import evaluate


class AuthorArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def make(self, filename='USData.mat', assets=None):
        international = filename == 'IntnlData.mat'
        assets = assets or (49 if international else 4)
        path = self.directory / filename
        with h5py.File(path, 'w') as f:
            f['N'] = np.array([[assets]], dtype=np.float64)
            f['T'] = np.array([[15]], dtype=np.float64)
            months = [199201 + month for month in range(12)] + [199301, 199302, 199303]
            stamps = months if international else [month * 100 + 28 for month in months]
            f['ym' if international else 'ymd'] = np.array([stamps], dtype=np.float64)
            for key in reader.CORE_FIELDS:
                dtype = np.float64 if international and key != 'DGW' else np.float32
                values = np.full((15, assets), 0.01 if key == 'Return' else 0.2 if key == 'DGW' else 100, dtype=dtype)
                f.create_dataset(key, data=values, chunks=(15, 2), compression='gzip')
            if international:
                f['NumAll'] = np.ones((1, 49), dtype=np.float64)
                codes = [chr(65 + i // 26) + chr(65 + i % 26) for i in range(49)]
                f['CountryCodes'] = np.array([[ord(code[j]) for code in codes] for j in range(2)], dtype=np.uint16)
        return path

    @contextmanager
    def pinned_fixture(self, path):
        """Only a test-local patch admits this synthetic fixture's identity."""
        data = path.read_bytes()
        source = contract.source_metadata(path.name)
        with h5py.File(path, 'r') as f:
            source.update(assets=int(f['N'][0, 0]), periods=int(f['T'][0, 0]))
        source.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                      repository_md5=hashlib.md5(data).hexdigest(), first_month='1992-01', last_month='1993-03')
        with patch.dict(contract.SOURCES, {path.name: source}):
            yield source

    def test_extract_exact_original_rows_and_observation_dates(self):
        path = self.make()
        with self.pinned_fixture(path):
            panel = reader.extract_panel(path, '1993-03', row_offset=1, row_count=2)
        self.assertEqual(panel['months'], ['1992-03', '1992-04', '1992-05', '1992-06', '1992-07', '1992-08',
                                         '1992-09', '1992-10', '1992-11', '1992-12', '1993-01', '1993-02', '1993-03'])
        self.assertEqual(panel['source_observation_dates'][0], '1992-03-28')
        self.assertEqual([row['source_row'] for row in panel['rows']], [2, 3])
        self.assertEqual([row['asset'] for row in panel['rows']], ['USData.mat:row:2', 'USData.mat:row:3'])
        self.assertTrue(all(row['country'] is None for row in panel['rows']))

    def test_nonfinite_states_zero_and_high_return_are_not_filled(self):
        path = self.make()
        with h5py.File(path, 'r+') as f:
            f['Return'][2, 0] = np.nan
            f['Return'][3, 0] = np.inf
            f['Return'][4, 0] = -np.inf
            f['Return'][14, 0] = 24.0
            f['DGW'][13, 0] = np.nan
            f['MV'][13, 0] = 0.0
        with self.pinned_fixture(path):
            row = reader.extract_panel(path, '1993-03', row_count=1)['rows'][0]
        self.assertEqual(row['return_states'][:3], ['nan', 'posinf', 'neginf'])
        self.assertEqual(row['returns'][:3], [None, None, None])
        self.assertEqual(row['returns'][-1], 24.0)
        self.assertEqual((row['dgw'], row['dgw_state']), (None, 'nan'))
        self.assertEqual((row['market_cap'], row['market_cap_state']), (0.0, 'value'))
        json.dumps(row, allow_nan=False)

    def test_country_boundaries_and_month_only_dates(self):
        path = self.make('IntnlData.mat')
        with self.pinned_fixture(path):
            panel = reader.extract_panel(path, '1993-03', row_offset=24, row_count=3)
        self.assertEqual([row['country'] for row in panel['rows']], ['AY', 'AZ', 'BA'])
        self.assertEqual(panel['source_observation_dates'], [None] * 13)

    def test_audit_counts_all_rows_and_future_labels_separately(self):
        path = self.make()
        with h5py.File(path, 'r+') as f:
            f['Return'][14, 0] = np.nan  # Formation remains ready for this row.
            f['MV'][13, 1] = 0
            f['DGW'][13, 2] = 2
            f['Return'][2, 3] = -2
        with self.pinned_fixture(path):
            audit = reader.audit(path)
            expected = evaluate(reader.extract_panel(path, '1993-03', row_count=4))['summary']
        observed = audit['alignment_diagnostics'][0]['summary']
        self.assertEqual(observed['formation_ready'], 1)
        self.assertEqual(observed['ready_with_label'], 0)
        self.assertEqual(observed['formation_ready_missing_label'], 1)
        self.assertEqual({key: observed[key] for key in expected}, expected)
        self.assertEqual(audit['quality']['Return']['below_minus_one'], 1)
        self.assertEqual(audit['quality']['Return']['nan'], 1)
        self.assertEqual(audit['quality']['DGW']['above_one'], 1)
        self.assertEqual(audit['quality']['MV']['zero'], 1)
        self.assertEqual(audit['alignment_diagnostics'][1]['status'], 'month_not_available')
        self.assertTrue(audit['raw_file_verified'])
        json.dumps(audit, allow_nan=False)

    def test_quality_reads_asset_blocks_not_whole_panel(self):
        class Dataset:
            shape, size, chunks = (15, 9), 135, (15, 2)
            reads = []

            def __getitem__(self, selection):
                self.reads.append(selection)
                self_width = min(9, selection[1].stop) - selection[1].start
                return np.ones((15, self_width))
        dataset = Dataset()
        with patch.object(reader, 'ROW_BLOCK', 4):
            quality = reader._quality(dataset, [str(i) for i in range(15)])
        self.assertEqual(quality['finite'], 135)
        self.assertEqual(len(dataset.reads), 3)
        self.assertTrue(all(x[1].stop - x[1].start <= 4 for x in dataset.reads))

    def test_fixed_size_and_both_digests_are_required_before_hdf_open(self):
        path = self.make()
        with self.assertRaisesRegex(ValueError, 'pinned'):
            reader.audit(path)
        for field in ('sha256', 'repository_md5', 'bytes'):
            with self.pinned_fixture(path) as source:
                changed = deepcopy(source)
                changed[field] = source[field] + 1 if field == 'bytes' else '0' * len(source[field])
                with patch.dict(contract.SOURCES, {path.name: changed}), patch('h5py.File', side_effect=AssertionError('HDF must not open')):
                    with self.assertRaises(ValueError):
                        reader.audit(path)

    def test_filesystem_symlink_and_hardlink_rejected(self):
        original = self.make()
        with self.pinned_fixture(original):
            other = self.directory / 'other'
            other.mkdir()
            link = other / original.name
            link.symlink_to(original)
            with self.assertRaises(ValueError):
                reader.audit(link)
            link.unlink()
            os.link(original, link)
            with self.assertRaises(ValueError):
                reader.audit(original)

    def test_hdf_link_types_and_duplicate_objects_rejected(self):
        for kind in ('soft', 'external', 'duplicate', 'group'):
            with self.subTest(kind=kind):
                path = self.make()
                with h5py.File(path, 'r+') as f:
                    if kind == 'soft':
                        f['Extra'] = h5py.SoftLink('/Return')
                    elif kind == 'external':
                        f['Extra'] = h5py.ExternalLink('/does/not/exist.h5', '/Return')
                    elif kind == 'duplicate':
                        f['Extra'] = f['Return']
                    else:
                        f.create_group('Extra')
                with self.pinned_fixture(path), self.assertRaisesRegex(ValueError, 'HDF'):
                    reader.audit(path)

    def test_external_virtual_and_unknown_filter_storage_rejected(self):
        for kind in ('external', 'virtual', 'plugin'):
            with self.subTest(kind=kind):
                path = self.make()
                with h5py.File(path, 'r+') as f:
                    if kind == 'external':
                        f.create_dataset('Extra', shape=(2, 2), dtype='float32', external=[('unread.bin', 0, h5py.h5f.UNLIMITED)])
                    elif kind == 'virtual':
                        layout = h5py.VirtualLayout(shape=(2, 2), dtype='float32')
                        layout[:] = h5py.VirtualSource('unread.h5', 'Return', shape=(2, 2))
                        f.create_virtual_dataset('Extra', layout)
                    else:
                        f.create_dataset('Extra', shape=(2, 2), dtype='float32', chunks=(2, 2),
                                         compression=32001, allow_unknown_filter=True)
                with self.pinned_fixture(path), self.assertRaisesRegex(ValueError, 'HDF'):
                    reader.audit(path)

    def test_bad_core_shape_dtype_month_axis_and_dates_rejected(self):
        for kind in ('shape', 'dtype', 'gap', 'duplicate', 'invalid_date', 'fractional_stamp', 'nonfinite_stamp'):
            with self.subTest(kind=kind):
                path = self.make()
                with h5py.File(path, 'r+') as f:
                    if kind in ('shape', 'dtype'):
                        del f['Return']
                        f['Return'] = np.ones((4, 15) if kind == 'shape' else (15, 4), dtype='float32' if kind == 'shape' else 'float64')
                    else:
                        value = {'gap': 19920528, 'duplicate': 19920228, 'invalid_date': 19920231,
                                 'fractional_stamp': 19920328.5, 'nonfinite_stamp': np.inf}[kind]
                        f['ymd'][0, 2] = value
                with self.pinned_fixture(path), self.assertRaises(ValueError):
                    reader.audit(path)

    def test_invalid_country_mapping_rejected(self):
        for kind in ('sum', 'fractional', 'duplicate_code', 'invalid_code'):
            with self.subTest(kind=kind):
                path = self.make('IntnlData.mat')
                with h5py.File(path, 'r+') as f:
                    if kind in ('sum', 'fractional'):
                        f['NumAll'][0, 0] = 2 if kind == 'sum' else 1.5
                    elif kind == 'duplicate_code':
                        f['CountryCodes'][:, 1] = f['CountryCodes'][:, 0]
                    else:
                        f['CountryCodes'][0, 0] = ord('1')
                with self.pinned_fixture(path), self.assertRaises(ValueError):
                    reader.audit(path)

    def test_input_bounds_and_missing_warmup_rejected(self):
        path = self.make()
        with self.pinned_fixture(path):
            for values in [('1993-03', True, 1), ('1993-03', 0, False), ('1993-03', -1, 1),
                           ('1993-03', 0, 513), ('1993-03', 3, 2), ('1992-03', 0, 1),
                           ('1993-13', 0, 1), ('1994-01', 0, 1)]:
                with self.subTest(values=values), self.assertRaises(ValueError):
                    reader.extract_panel(path, *values)

    def test_mutation_during_read_is_detected_before_return(self):
        path = self.make()
        original = reader._quality
        mutated = False

        def touch(*args):
            nonlocal mutated
            value = original(*args)
            if not mutated:
                stamp = path.stat()
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
                mutated = True
            return value

        with self.pinned_fixture(path), patch.object(reader, '_quality', side_effect=touch):
            with self.assertRaisesRegex(ValueError, 'changed'):
                reader.audit(path)

    def test_replacement_during_read_is_detected(self):
        path = self.make()
        original = reader._quality
        replaced = False

        def replace(*args):
            nonlocal replaced
            value = original(*args)
            if not replaced:
                replacement = self.directory / 'replacement.mat'
                replacement.write_bytes(path.read_bytes())
                replacement.replace(path)
                replaced = True
            return value

        with self.pinned_fixture(path), patch.object(reader, '_quality', side_effect=replace):
            with self.assertRaisesRegex(ValueError, 'changed'):
                reader.audit(path)


if __name__ == '__main__':
    unittest.main()
