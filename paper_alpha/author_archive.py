"""Read only the two pinned, perturbed GJS author archives; never fetch data."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import math
import os
from pathlib import Path
import re
import stat

import numpy as np

from .author_archive_contract import ADAPTER_VERSION, LIMITATIONS, MAX_ROWS, source_metadata

CORE_FIELDS = ('Return', 'DGW', 'MV')
AUDIT_MONTHS = ('1993-03', '2000-01', '2015-01', '2020-01')
ROW_BLOCK = 512
HASH_BLOCK = 1024 * 1024


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _guard(path, handle, original):
    current = path.lstat()
    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
            or _identity(current) != original or _identity(os.fstat(handle.fileno())) != original):
        raise ValueError('Author archive changed during verification or reading')


def _inspect_hdf(archive, source):
    """Inspect links/filters before reading any HDF dataset, including metadata."""
    import h5py

    names = list(archive.keys())
    if not 4 <= len(names) <= 64:
        raise ValueError('Unexpected author HDF inventory')
    addresses, inventory = set(), {}
    for name in names:
        if not isinstance(archive.get(name, getlink=True), h5py.HardLink):
            raise ValueError('Author HDF soft or external links are not supported')
        dataset = archive[name]
        if not isinstance(dataset, h5py.Dataset):
            raise ValueError('Author HDF groups and nested links are not supported')
        address = h5py.h5o.get_info(dataset.id).addr
        if address in addresses:
            raise ValueError('Author HDF duplicate hard links are not supported')
        addresses.add(address)
        if dataset.is_virtual or dataset.external:
            raise ValueError('Author HDF virtual or external storage is not supported')
        if (dataset.dtype.kind not in 'fiu' or dataset.dtype.fields is not None
                or dataset.ndim != 2 or any(size <= 0 for size in dataset.shape)
                or max(dataset.shape) > max(source['assets'], source['periods'], 49)
                or dataset.size > max(source['assets'] * source['periods'], 98)):
            raise ValueError('Unexpected author HDF dataset type or dimensions')
        properties = dataset.id.get_create_plist()
        filters = [properties.get_filter(i)[0] for i in range(properties.get_nfilters())]
        # The pinned archives use only built-in DEFLATE. Never invoke plugin filters.
        if any(identity != 1 for identity in filters):
            raise ValueError('Author HDF filter is not in the pinned built-in allowlist')
        inventory[name] = {'shape': list(dataset.shape), 'dtype': str(dataset.dtype),
                           'chunks': list(dataset.chunks) if dataset.chunks else None,
                           'filter_ids': filters}
    return inventory


def _small(archive, name, shape, dtype):
    if name not in archive or archive[name].shape != shape or archive[name].dtype != np.dtype(dtype):
        raise ValueError(f'Unexpected author metadata shape or dtype: {name}')
    return archive[name][:]


def _metadata(archive, source):
    periods, assets = source['periods'], source['assets']
    for key, value in (('N', assets), ('T', periods)):
        actual = _small(archive, key, (1, 1), 'float64').item()
        if not math.isfinite(actual) or actual != value:
            raise ValueError(f'Author {key} differs from pinned dimensions')
    international = source['filename'] == 'IntnlData.mat'
    for name in CORE_FIELDS:
        expected_type = 'float64' if international and name != 'DGW' else 'float32'
        if (name not in archive or archive[name].shape != (periods, assets)
                or archive[name].dtype != np.dtype(expected_type)):
            raise ValueError(f'Unexpected author core shape or dtype: {name}')
    stamp_name = 'ym' if international else 'ymd'
    raw = _small(archive, stamp_name, (1, periods), 'float64').ravel()
    months, observed, ordinal = [], [], []
    for stamp in raw:
        if not math.isfinite(stamp) or stamp != int(stamp):
            raise ValueError('Author time stamps must be finite integers')
        number = int(stamp)
        year, month = (number // 100, number % 100) if international else (number // 10000, number // 100 % 100)
        try:
            parsed = date(year, month, 1 if international else number % 100)
        except ValueError as exc:
            raise ValueError('Invalid author observation month or date') from exc
        months.append(f'{year:04d}-{month:02d}')
        observed.append(None if international else parsed.isoformat())
        ordinal.append(year * 12 + month - 1)
    if (months[0] != source['first_month'] or months[-1] != source['last_month']
            or any(right != left + 1 for left, right in zip(ordinal, ordinal[1:]))):
        raise ValueError('Author month axis differs from its contiguous pinned range')
    countries, country_ends = [], []
    if international:
        counts = _small(archive, 'NumAll', (1, 49), 'float64').ravel()
        if (not np.all(np.isfinite(counts)) or np.any(counts <= 0)
                or np.any(counts != np.floor(counts)) or float(counts.sum()) != assets):
            raise ValueError('Author country row counts do not cover the asset axis')
        chars = _small(archive, 'CountryCodes', (2, 49), 'uint16')
        countries = [''.join(chr(int(code)) for code in chars[:, i]) for i in range(49)]
        if len(set(countries)) != 49 or any(re.fullmatch(r'[A-Z]{2}', name) is None for name in countries):
            raise ValueError('Author country codes must be 49 unique two-letter codes')
        country_ends = np.cumsum(counts.astype(np.int64)).tolist()
    return {'months': months, 'source_observation_dates': observed,
            'countries': countries, 'country_row_ends': country_ends}


@contextmanager
def _verified_archive(path):
    try:
        import h5py
    except ImportError as exc:
        raise ValueError('Author MAT tools require the pinned h5py archive dependency') from exc
    path = Path(path).absolute()
    source = source_metadata(path.name)
    initial = path.lstat()
    if (path.resolve() != path or not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1
            or initial.st_size != source['bytes']):
        raise ValueError('Author archive must be the bounded pinned independent regular file')
    original = _identity(initial)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        _guard(path, handle, original)
        sha, md5 = hashlib.sha256(), hashlib.md5()
        remaining = source['bytes']
        while remaining:
            chunk = handle.read(min(HASH_BLOCK, remaining))
            if not chunk:
                raise ValueError('Author archive changed while hashing')
            sha.update(chunk)
            md5.update(chunk)
            remaining -= len(chunk)
        if handle.read(1):
            raise ValueError('Author archive grew while hashing')
        _guard(path, handle, original)
        if sha.hexdigest() != source['sha256'] or md5.hexdigest() != source['repository_md5']:
            raise ValueError('Author archive SHA256 or repository MD5 mismatch')
        handle.seek(0)
        try:
            with h5py.File(handle, 'r') as archive:
                inventory = _inspect_hdf(archive, source)
                metadata = _metadata(archive, source)
                yield archive, source, inventory, metadata
        finally:
            _guard(path, handle, original)


def _blocks(dataset):
    # HDF MATLAB chunks span the time axis. Block assets to avoid repeatedly
    # decompressing all historical months for each time-slice read.
    chunk_rows = dataset.chunks[1] if dataset.chunks else 1
    width = max(1, ROW_BLOCK // chunk_rows) * chunk_rows if chunk_rows <= ROW_BLOCK else ROW_BLOCK
    for start in range(0, dataset.shape[1], width):
        yield start, np.asarray(dataset[:, start:start + width], dtype=np.float64)


def _quality(dataset, months):
    result = {'observations': int(dataset.size), 'finite': 0, 'nan': 0, 'posinf': 0, 'neginf': 0,
              'zero': 0, 'negative': 0, 'below_minus_one': 0, 'above_one': 0, 'min': None, 'max': None}
    monthly_finite = np.zeros(dataset.shape[0], dtype=np.int64)
    for _, block in _blocks(dataset):
        finite = np.isfinite(block)
        conditions = {'finite': finite, 'nan': np.isnan(block), 'posinf': np.isposinf(block),
                      'neginf': np.isneginf(block), 'zero': block == 0,
                      'negative': finite & (block < 0), 'below_minus_one': finite & (block < -1),
                      'above_one': finite & (block > 1)}
        for key, mask in conditions.items():
            result[key] += int(np.count_nonzero(mask))
        monthly_finite += np.count_nonzero(finite, axis=1)
        values = block[finite]
        if values.size:
            low, high = float(values.min()), float(values.max())
            result['min'] = low if result['min'] is None else min(result['min'], low)
            result['max'] = high if result['max'] is None else max(result['max'], high)
    result['monthly_finite'] = [{'month': month, 'finite': int(count)} for month, count in zip(months, monthly_finite)]
    result['all_missing_months'] = [month for month, count in zip(months, monthly_finite) if count == 0]
    return result


def _diagnostic(archive, metadata, target):
    months = metadata['months']
    if target not in months:
        return {'target_month': target, 'status': 'month_not_available'}
    index = months.index(target)
    if index < 12:
        return {'target_month': target, 'status': 'insufficient_history'}
    counts = dict.fromkeys(('assets', 'momentum_complete', 'dgw_usable', 'market_cap_usable',
                           'formation_ready', 'labels_available', 'ready_with_label', 'formation_ready_missing_label'), 0)
    for start in range(0, archive['Return'].shape[1], ROW_BLOCK):
        stop = start + ROW_BLOCK
        history = np.asarray(archive['Return'][index - 12:index - 1, start:stop], dtype=np.float64)
        dgw = np.asarray(archive['DGW'][index - 1, start:stop], dtype=np.float64)
        mv = np.asarray(archive['MV'][index - 1, start:stop], dtype=np.float64)
        label = np.asarray(archive['Return'][index, start:stop], dtype=np.float64)
        complete = np.all(np.isfinite(history) & (history >= -1), axis=0)
        representable = np.zeros(len(dgw), dtype=bool)
        for position in np.flatnonzero(complete):
            values = history[:, position]
            try:
                momentum = (-1.0 if np.any(values == -1) else
                            math.expm1(math.fsum(math.log1p(float(value)) for value in values)))
                representable[position] = math.isfinite(momentum)
            except OverflowError:
                pass
        good_dgw = np.isfinite(dgw) & (np.abs(dgw) <= 1)
        good_mv = np.isfinite(mv) & (mv > 0)
        formation = complete & representable & good_dgw & good_mv
        good_label = np.isfinite(label) & (label >= -1)
        counts['assets'] += len(dgw)
        for key, mask in [('momentum_complete', complete), ('dgw_usable', good_dgw),
                          ('market_cap_usable', good_mv), ('formation_ready', formation),
                          ('labels_available', good_label), ('ready_with_label', formation & good_label),
                          ('formation_ready_missing_label', formation & ~good_label)]:
            counts[key] += int(np.count_nonzero(mask))
    return {'target_month': target, 'status': 'diagnosed',
            'momentum_months': months[index - 12:index - 1], 'formation_month': months[index - 1],
            'label_month': months[index], 'summary': counts,
            'scope': 'all original rows; diagnostic availability only; no REIT, microcap or portfolio filters'}


def audit(path):
    """Verify an entire pinned file and return bounded, block-scanned diagnostics."""
    with _verified_archive(path) as (archive, source, inventory, metadata):
        observations = metadata['source_observation_dates']
        non_month_end = []
        for value in observations:
            if value is not None:
                current = date.fromisoformat(value)
                next_month = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
                if (next_month - current).days != 1:
                    non_month_end.append(value)
        result = {'schema_version': 1, 'adapter_version': ADAPTER_VERSION, 'raw_file_verified': True,
                  'source': source, 'verification': ['pinned_size', 'repository_md5', 'sha256',
                      'independent_regular_file', 'safe_hdf_layout', 'before_after_source_identity'],
                  'datasets': inventory,
                  'time': {'periods': len(metadata['months']), 'months': metadata['months'],
                           'source_observation_dates': observations, 'contiguous_months': True,
                           'non_calendar_month_end_dates': non_month_end},
                  'country_mapping': {'codes': metadata['countries'], 'row_ends': metadata['country_row_ends']},
                  'quality': {field: _quality(archive[field], metadata['months']) for field in CORE_FIELDS},
                  'alignment_diagnostics': [_diagnostic(archive, metadata, target) for target in AUDIT_MONTHS],
                  'limitations': list(LIMITATIONS)}
    return result


def _value(value):
    value = float(value)
    if math.isnan(value):
        return None, 'nan'
    if math.isinf(value):
        return None, 'posinf' if value > 0 else 'neginf'
    return value, 'value'


def extract_panel(path, target_month, row_offset=0, row_count=128):
    """Extract contiguous original rows; no selection by signal or label quality."""
    if not isinstance(target_month, str) or re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', target_month) is None:
        raise ValueError('target_month must be YYYY-MM')
    if type(row_offset) is not int or row_offset < 0 or type(row_count) is not int or not 1 <= row_count <= MAX_ROWS:
        raise ValueError('Author selection requires a nonnegative offset and 1..512 rows')
    with _verified_archive(path) as (archive, source, _, metadata):
        if row_offset + row_count > source['assets']:
            raise ValueError('Author selection exceeds original asset rows')
        if target_month not in metadata['months'] or metadata['months'].index(target_month) < 12:
            raise ValueError('Author target month requires 12 preceding source months')
        index = metadata['months'].index(target_month)
        selection = slice(row_offset, row_offset + row_count)
        returns = archive['Return'][index - 12:index + 1, selection]
        dgw = archive['DGW'][index - 1, selection]
        market_cap = archive['MV'][index - 1, selection]
        rows = []
        for local in range(row_count):
            original = row_offset + local + 1
            normalized = [_value(value) for value in returns[:, local]]
            id_value, id_state = _value(dgw[local])
            cap_value, cap_state = _value(market_cap[local])
            country_index = int(np.searchsorted(metadata['country_row_ends'], original, side='left'))
            country = metadata['countries'][country_index] if metadata['countries'] else None
            rows.append({'source_row': original, 'asset': f"{source['filename']}:row:{original}", 'country': country,
                         'returns': [value for value, _ in normalized],
                         'return_states': [state for _, state in normalized], 'dgw': id_value, 'dgw_state': id_state,
                         'market_cap': cap_value, 'market_cap_state': cap_state})
        panel = {'schema_version': 1, 'kind': 'author_perturbed_monthly_panel', 'adapter_version': ADAPTER_VERSION,
                 'source': source, 'selection': {'target_month': target_month, 'row_offset': row_offset, 'row_count': row_count},
                 'months': metadata['months'][index - 12:index + 1],
                 'source_observation_dates': metadata['source_observation_dates'][index - 12:index + 1], 'rows': rows}
        from .author_panel import validate_panel
        validate_panel(panel)
    return panel
