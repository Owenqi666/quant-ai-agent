"""Explicit synthetic-format fixtures only; no real-source or investment claim."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED

from paper_alpha import french_industry as source


class FrenchIndustryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / 'synthetic.zip'
        self.columns = list(source.ASSETS)

    def csv(self, rows=None, *, columns=None, heading=None, title=None, suffix=''):
        rows = rows or [('200901', ['1.25'] * 49), ('200902', ['-2.50'] * 49), ('200903', ['0.00'] * 49)]
        preamble = ['This file was created using the 202608 CRSP database.',
                    source.TITLE if title is None else title, 'Missing data are indicated by -99.99 or -999.', '',
                    heading or source.SECTION, ',' + ','.join(columns or self.columns)]
        return ('\r\n'.join(preamble + [month + ',' + ','.join(values) for month, values in rows])
                + '\r\n\r\n' + suffix).encode()

    def archive(self, data=None, *, filename='49_Industry_Portfolios.csv', extra=False, symlink=False):
        buffer = BytesIO()
        with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as zipfile:
            if symlink:
                member = ZipInfo(filename)
                member.create_system = 3
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipfile.writestr(member, data or self.csv())
            else:
                zipfile.writestr(filename, data or self.csv())
            if extra:
                zipfile.writestr('README.txt', 'synthetic extra fixture')
        self.path.write_bytes(buffer.getvalue())
        return sha256(buffer.getvalue()).hexdigest()

    def parse(self, expected=None, **kwargs):
        expected = expected or self.archive()
        return source.parse_archive(self.path, '2009-01', '2009-03', expected, **kwargs)

    def test_closed_source_specific_panel_and_percent_conversion(self):
        raw = self.csv()
        expected = self.archive(raw)
        diagnostics = {}
        panel = self.parse(expected, diagnostics_out=diagnostics)
        self.assertEqual(set(panel), {'schema_version','data_kind','source_id','asset_kind','return_semantics','months','assets','returns','source'})
        self.assertEqual(set(panel['source']), {'archive_sha256','csv_sha256','download_url','section','source_contract','header_preamble'})
        self.assertEqual(panel['source']['csv_sha256'], sha256(raw).hexdigest())
        self.assertEqual(panel['source']['archive_sha256'], expected)
        self.assertEqual(panel['source']['download_url'], source.DOWNLOAD_URL)
        self.assertEqual(panel['asset_kind'], 'industry_portfolio')
        self.assertEqual(panel['return_semantics'], 'monthly_total_return_decimal')
        self.assertEqual(panel['months'], ['2009-01','2009-02','2009-03'])
        self.assertEqual(panel['assets'], list(source.ASSETS))
        self.assertEqual(len(panel['returns']), 147)
        self.assertEqual(panel['returns'][0], {'month':'2009-01','asset':'Agric','value':0.0125})
        self.assertEqual(panel['returns'][49]['value'], -0.025)
        self.assertEqual(diagnostics['unselected_numeric_tokens_parsed'], 0)
        self.assertFalse(diagnostics['missing_values_imputed'])

    def test_sentinels_exactly_before_conversion_blank_null_and_total_loss_valid(self):
        values = ['-99.99','-999','-99.990','-999.0','', '  ', '-100', '-99.98'] + ['0'] * 41
        diagnostics = {}
        expected = self.archive(self.csv(rows=[('200901',values)]))
        panel = self.parse(expected, diagnostics_out=diagnostics)
        self.assertEqual([row['value'] for row in panel['returns'][:8]], [None,None,None,None,None,None,-1.0,-0.9998])
        self.assertEqual(len(diagnostics['missing_values']), 6)
        self.assertEqual(diagnostics['missing_values'][0]['reason'], 'source_sentinel_-99.99')
        self.assertEqual(diagnostics['missing_values'][1]['reason'], 'source_sentinel_-999')
        self.assertEqual(diagnostics['missing_values'][4]['reason'], 'source_blank')

    def test_missing_months_are_declared_without_generated_rows(self):
        expected = self.archive(self.csv(rows=[('200901',['1']*49),('200903',['2']*49)]))
        diagnostics = {}
        panel = self.parse(expected, diagnostics_out=diagnostics)
        self.assertEqual(panel['months'], ['2009-01','2009-02','2009-03'])
        self.assertEqual(len(panel['returns']), 98)
        self.assertFalse(any(row['month'] == '2009-02' for row in panel['returns']))
        self.assertEqual(diagnostics['missing_months'], [{'month':'2009-02','reason':'missing_row'}])

    def test_no_numeric_parsing_before_window_or_after_requested_end(self):
        expected = self.archive(self.csv(rows=[('200812',['invalid-before-window']*49),
            ('200901',['1']*49),('200902',['2']*49),('200903',['3']*49),('200904',['NaN']*49),('201201',['reserved-token-not-parsed']*49)]))
        original = source._parse_value
        seen = []
        def observe(raw, month, asset, missing):
            seen.append(month)
            return original(raw, month, asset, missing)
        with patch.object(source, '_parse_value', side_effect=observe):
            panel = self.parse(expected)
        self.assertEqual(set(seen), {'2009-01','2009-02','2009-03'})
        self.assertEqual(len(seen), 147)
        self.assertEqual(len(panel['returns']), 147)

    def test_equal_weighted_and_annual_values_never_used(self):
        suffix = '\n'.join(['Average Equal Weighted Returns -- Monthly', ',' + ','.join(self.columns),
                            '200901,' + ','.join(['999']*49), '', 'Average Value Weighted Returns -- Annual',
                            ',' + ','.join(self.columns), '2009,' + ','.join(['999']*49)])
        panel = self.parse(self.archive(self.csv(suffix=suffix)))
        self.assertEqual(panel['returns'][0]['value'], 0.0125)
        self.assertFalse(any(row['value'] == 9.99 for row in panel['returns']))
        with self.assertRaises(source.FrenchSourceError):
            self.parse(self.archive(self.csv(heading='Average Equal Weighted Returns -- Monthly')))

    def test_duplicate_or_reversed_selected_dates_rejected(self):
        for dates in (('200901','200901'),('200902','200901')):
            with self.subTest(dates=dates), self.assertRaisesRegex(source.FrenchSourceError,'order'):
                self.parse(self.archive(self.csv(rows=[(date,['1']*49) for date in dates])))

    def test_malformed_date_or_row_width_rejected(self):
        for date in ('200913','200900','000001','2009-01','20090101'):
            with self.subTest(date=date), self.assertRaises(source.FrenchSourceError):
                self.parse(self.archive(self.csv(rows=[(date,['1']*49)])))
        for count in (48,50):
            with self.subTest(count=count), self.assertRaisesRegex(source.FrenchSourceError,'49 fields'):
                self.parse(self.archive(self.csv(rows=[('200901',['1']*count)])))

    def test_wrong_duplicate_empty_or_reordered_industry_names_rejected(self):
        variants = [self.columns[:-1], self.columns + ['Extra'], ['A'+str(i) for i in range(49)],
                    [self.columns[0]]*49, ['']+self.columns[1:], list(reversed(self.columns))]
        for columns in variants:
            with self.subTest(columns=columns[:2]), self.assertRaises(source.FrenchSourceError):
                self.parse(self.archive(self.csv(columns=columns)))

    def test_nonfinite_unrepresentable_and_below_minus_one_rejected(self):
        for token in ('NaN','Infinity','-Inf','-100.01','-1000','1e999','1e-999','not-number'):
            values = [token]+['0']*48
            with self.subTest(token=token), self.assertRaises(source.FrenchSourceError):
                self.parse(self.archive(self.csv(rows=[('200901',values)])))

    def test_bad_hash_and_source_title_rejected(self):
        self.archive()
        for expected in ('0'*64, 'A'*64, '', None, True):
            with self.subTest(expected=expected), self.assertRaises(source.FrenchSourceError):
                source.parse_archive(self.path,'2009-01','2009-03',expected)
        with self.assertRaises(source.FrenchSourceError):
            self.parse(self.archive(self.csv(title='It contains 49 stock returns.')))

    def test_duplicate_monthly_vw_section_rejected(self):
        suffix = source.SECTION + '\n,' + ','.join(self.columns) + '\n200901,' + ','.join(['1']*49)
        with self.assertRaisesRegex(source.FrenchSourceError,'one explicitly'):
            self.parse(self.archive(self.csv(suffix=suffix)))

    def test_no_extraction_unsafe_extra_and_symlink_members_rejected(self):
        for filename in ('../49_Industry_Portfolios.csv', '/49_Industry_Portfolios.csv', 'nested/49_Industry_Portfolios.csv', 'other.csv'):
            with self.subTest(filename=filename), self.assertRaises(source.FrenchSourceError):
                self.parse(self.archive(filename=filename))
        for kwargs in ({'extra':True},{'symlink':True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(source.FrenchSourceError):
                self.parse(self.archive(**kwargs))
        with (patch.object(ZipFile,'extract',side_effect=AssertionError('No extraction')),
              patch.object(ZipFile,'extractall',side_effect=AssertionError('No extraction'))):
            self.assertEqual(len(self.parse()['returns']), 147)

    def test_source_symlink_and_hardlink_rejected(self):
        expected = self.archive()
        other = self.root / 'linked.zip'
        other.symlink_to(self.path)
        with self.assertRaises(source.FrenchSourceError):
            source.parse_archive(other,'2009-01','2009-03',expected)
        other.unlink()
        os.link(self.path, other)
        with self.assertRaises(source.FrenchSourceError):
            self.parse(expected)

    def test_archive_expansion_line_and_month_budgets(self):
        expected = self.archive()
        with patch.object(source,'MAX_ARCHIVE_BYTES',10), self.assertRaises(source.FrenchSourceError):
            self.parse(expected)
        with patch.object(source,'MAX_CSV_BYTES',10), self.assertRaises(source.FrenchSourceError):
            self.parse(expected)
        with patch.object(source,'MAX_LINE_BYTES',10), self.assertRaises(source.FrenchSourceError):
            self.parse(expected)
        for start,end in (('2009-03','2009-01'),('0000-01','2009-01'),('2009-00','2009-01'),('2009-1','2009-01'),('1900-01','2011-12')):
            with self.subTest(start=start,end=end), self.assertRaises(source.FrenchSourceError):
                source.parse_archive(self.path,start,end,expected)

    def test_corrupt_zip_or_invalid_utf8_and_numeric_token_budget_rejected(self):
        for raw in (b'not-zip', b'PK\x03\x04truncated'):
            self.path.write_bytes(raw)
            with self.assertRaises(source.FrenchSourceError):
                self.parse(sha256(raw).hexdigest())
        with self.assertRaises(source.FrenchSourceError):
            self.parse(self.archive(b'\xffinvalid utf8'))
        values = ['1'*129]+['0']*48
        with self.assertRaisesRegex(source.FrenchSourceError,'128 characters'):
            self.parse(self.archive(self.csv(rows=[('200901',values)])))

    def test_diagnostics_are_sidecar_and_only_published_on_success(self):
        for value in ([], {'old':'do not erase'}):
            original = deepcopy(value)
            with self.assertRaises(source.FrenchSourceError):
                self.parse(diagnostics_out=value)
            self.assertEqual(value,original)
        diagnostics = {}
        with self.assertRaises(source.FrenchSourceError):
            self.parse(self.archive(self.csv(rows=[('200901',['NaN']*49)])),diagnostics_out=diagnostics)
        self.assertEqual(diagnostics,{})


if __name__ == '__main__':
    unittest.main()
