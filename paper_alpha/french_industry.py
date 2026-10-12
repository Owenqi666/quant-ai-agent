"""Bounded offline reader for one Kenneth French industry-return file.

This is a source-specific conversion, not stock prices, a data downloader, or a
paper-replication adapter. Caller-provided hashes verify bytes, not authenticity.
Only explicitly selected monthly VW numeric fields are parsed. The complete
archive/member hashes still include later source bytes and do not imply blind
or point-in-time data. No archive member is extracted onto the filesystem.
"""
import csv
from decimal import Decimal, DecimalException, localcontext
from hashlib import sha256
from io import BytesIO
import math
import os
from pathlib import Path
import re
import stat
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

DOWNLOAD_URL = 'https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/49_Industry_Portfolios_CSV.zip'
SECTION = 'Average Value Weighted Returns -- Monthly'
SOURCE_ID = 'kenneth-french-49-industry-monthly-value-weighted'
SOURCE_CONTRACT = 'kenneth-french-49-value-weighted-monthly-v1'
ASSETS = ('Agric', 'Food', 'Soda', 'Beer', 'Smoke', 'Toys', 'Fun', 'Books', 'Hshld', 'Clths',
          'Hlth', 'MedEq', 'Drugs', 'Chems', 'Rubbr', 'Txtls', 'BldMt', 'Cnstr', 'Steel', 'FabPr',
          'Mach', 'ElcEq', 'Autos', 'Aero', 'Ships', 'Guns', 'Gold', 'Mines', 'Coal', 'Oil',
          'Util', 'Telcm', 'PerSv', 'BusSv', 'Hardw', 'Softw', 'Chips', 'LabEq', 'Paper', 'Boxes',
          'Trans', 'Whlsl', 'Rtail', 'Meals', 'Banks', 'Insur', 'RlEst', 'Fin', 'Other')
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024
MAX_MONTHS = 240
TITLE = 'It contains value- and equal-weighted returns for 49 industry portfolios.'
MISSING_CODES = {Decimal('-99.99'): 'source_sentinel_-99.99', Decimal('-999'): 'source_sentinel_-999'}


class FrenchSourceError(ValueError):
    pass


def _month_index(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value) or value[:4] == '0000':
        raise FrenchSourceError('Month must be a valid YYYY-MM calendar month')
    return int(value[:4]) * 12 + int(value[5:]) - 1


def _months(start, end):
    first, last = _month_index(start), _month_index(end)
    if last < first or last - first + 1 > MAX_MONTHS:
        raise FrenchSourceError('Selected window must contain 1..240 ordered calendar months')
    return [f'{value // 12:04d}-{value % 12 + 1:02d}' for value in range(first, last + 1)]


def _read_archive(path, expected):
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise FrenchSourceError('Expected archive SHA256 must be an explicit lowercase 64-hex value')
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise FrenchSourceError('Source paths may not contain symbolic links')
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_ARCHIVE_BYTES:
                raise FrenchSourceError('Archive must be regular, unlinked and at most 8 MiB')
            raw = stream.read(MAX_ARCHIVE_BYTES + 1)
            after = os.fstat(stream.fileno())
            if len(raw) > MAX_ARCHIVE_BYTES or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise FrenchSourceError('Archive changed or exceeded its read bound')
    except OSError as exc:
        raise FrenchSourceError('Cannot safely read the bounded source archive') from exc
    if sha256(raw).hexdigest() != expected:
        raise FrenchSourceError('Archive bytes do not match the expected SHA256')
    return raw


def _csv_bytes(raw):
    try:
        with ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise FrenchSourceError('Expected exactly one source CSV member')
            member = members[0]
            mode = member.external_attr >> 16
            if (member.filename.lower() != '49_industry_portfolios.csv' or member.is_dir()
                    or stat.S_ISLNK(mode) or member.flag_bits & 1
                    or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                    or member.file_size > MAX_CSV_BYTES):
                raise FrenchSourceError('Unexpected, unsafe or excessive source member')
            with archive.open(member) as stream:
                value = stream.read(MAX_CSV_BYTES + 1)
            if len(value) > MAX_CSV_BYTES or len(value) != member.file_size:
                raise FrenchSourceError('Expanded source CSV exceeds 16 MiB or its declared size')
            return value
    except (BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
        raise FrenchSourceError('Invalid or unsupported bounded source ZIP') from exc


def _parse_value(raw, month, asset, missing):
    token = raw.strip()
    if not token:
        missing.append({'month': month, 'asset': asset, 'raw_marker': token, 'reason': 'source_blank'})
        return None
    if len(token) > 128:
        raise FrenchSourceError('Source numeric token exceeds 128 characters')
    try:
        value = Decimal(token)
        if not value.is_finite():
            raise FrenchSourceError('Selected return is nonfinite')
        if value in MISSING_CODES:
            missing.append({'month': month, 'asset': asset, 'raw_marker': token, 'reason': MISSING_CODES[value]})
            return None
        if value < -100:
            raise FrenchSourceError('Selected monthly simple return is below -100 percent')
        with localcontext() as context:
            context.prec = 160
            decimal_return = value / Decimal(100)
            normalized = float(decimal_return)
        if not math.isfinite(normalized) or normalized < -1:
            raise FrenchSourceError('Selected monthly decimal return is invalid or unrepresentable')
        if decimal_return != 0 and normalized == 0:
            raise FrenchSourceError('Selected monthly return underflows float representation')
        return normalized
    except (DecimalException, OverflowError) as exc:
        raise FrenchSourceError('Invalid selected source numeric return') from exc


def parse_archive(path, start_month, end_month, expected_sha256, *, diagnostics_out=None):
    """Return the closed panel DTO; optional dict receives missing-value facts.

    Missing months stay on panel.months with no generated return rows. Sentinels
    and blank fields remain null rows. No imputation, selection, strategy return,
    network access, file extraction or filesystem mutation is performed.
    """
    months = _months(start_month, end_month)
    if diagnostics_out is not None and (type(diagnostics_out) is not dict or diagnostics_out):
        raise FrenchSourceError('diagnostics_out must be a fresh empty dictionary')
    archive = _read_archive(path, expected_sha256)
    raw_csv = _csv_bytes(archive)
    try:
        source = raw_csv.decode('utf-8-sig')
    except UnicodeError as exc:
        raise FrenchSourceError('Source CSV must be bounded UTF-8/ASCII text') from exc
    lines = source.splitlines()
    if len(lines) > 50000 or any(len(line.encode('utf-8')) > MAX_LINE_BYTES for line in lines):
        raise FrenchSourceError('Source CSV exceeds its row/line budget')
    section_indices = [index for index, line in enumerate(lines) if line.strip() == SECTION]
    if len(section_indices) != 1 or section_indices[0] > 100:
        raise FrenchSourceError('Expected one explicitly titled monthly value-weighted section')
    start = section_indices[0]
    preamble = [line.strip() for line in lines[:start]]
    if (TITLE not in preamble or not preamble or not re.fullmatch(
            r'This file was created using the [0-9]{6} CRSP database\.', preamble[0])):
        raise FrenchSourceError('Unexpected 49-industry source title/preamble')
    if start + 1 >= len(lines):
        raise FrenchSourceError('Missing industry column header')
    header = [part.strip() for part in next(csv.reader([lines[start + 1]]))]
    if len(header) != 50 or header[0] != '' or tuple(header[1:]) != ASSETS:
        raise FrenchSourceError('Expected the canonical 49 industry column names and order')
    returns, observed, missing = [], [], []
    previous = None
    for line in lines[start + 2:]:
        if not line.strip():
            break
        # Determine date eligibility before parsing or validating numeric cells.
        date = line.split(',', 1)[0].strip()
        if not re.fullmatch(r'[0-9]{4}(0[1-9]|1[0-2])', date) or date[:4] == '0000':
            raise FrenchSourceError('Malformed source YYYYMM in the monthly VW section')
        month = date[:4] + '-' + date[4:]
        if previous is not None and month <= previous:
            raise FrenchSourceError('Duplicate or out-of-order source monthly date')
        previous = month
        if month > end_month:
            break
        if month < start_month:
            continue
        cells = next(csv.reader([line]))
        if len(cells) != 50:
            raise FrenchSourceError('Selected monthly return row must contain 49 fields')
        observed.append(month)
        for asset, raw in zip(ASSETS, cells[1:]):
            value = _parse_value(raw, month, asset, missing)
            returns.append({'month': month, 'asset': asset, 'value': value})
        if month == end_month:
            break  # Do not inspect the following held-out row's numeric tokens.
    observed_set = set(observed)
    missing_months = [month for month in months if month not in observed_set]
    panel = {'schema_version': 1, 'data_kind': 'market_derived_portfolio_returns',
        'source_id': SOURCE_ID, 'asset_kind': 'industry_portfolio',
        'return_semantics': 'monthly_total_return_decimal', 'months': months,
        'assets': list(ASSETS), 'returns': returns,
        'source': {'archive_sha256': expected_sha256, 'csv_sha256': sha256(raw_csv).hexdigest(),
                   'download_url': DOWNLOAD_URL, 'section': SECTION,
                   'source_contract': SOURCE_CONTRACT, 'header_preamble': preamble}}
    if diagnostics_out is not None:
        diagnostics_out.update({'schema_version': 1, 'scope': 'Offline source conversion only; caller hash is not source authentication.',
            'requested_start': start_month, 'requested_end': end_month,
            'observed_months': observed, 'missing_months': [{'month': month, 'reason': 'missing_row'} for month in missing_months],
            'missing_values': missing, 'observed_rows': len(returns),
            'raw_units': 'percent', 'internal_units': 'decimal', 'missing_values_imputed': False,
            'unselected_numeric_tokens_parsed': 0,
            'archive_contains_unselected_bytes': True,
            'selection_scope': 'Raw ZIP/member bytes retained and hashed; only requested monthly VW numeric tokens parsed. No blind-test assertion.'})
    return panel
