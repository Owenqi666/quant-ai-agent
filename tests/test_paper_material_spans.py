"""Pure span catalogue correctness; no workspace, network or model calls."""
from copy import deepcopy
import hashlib
import unittest

from paper_alpha.storage import digest
from paper_alpha.server.paper_material_spans import (CITATION_MODE, build_span_catalog,
                                                    validate_context_mode, validate_span_catalog)
from paper_alpha.server.paper_segments import MaterialError, segment_document
from tests.test_paper_segments import document


class PaperMaterialSpanTests(unittest.TestCase):
    def selection(self, texts=None, pages=None, **kwargs):
        texts = texts or ['Ａ  中文 café 😀 ' + 'repeated word ' * 20, 'UNSELECTED_SENTINEL']
        return segment_document(document(texts), pages or [1], **kwargs)

    def context(self, selection=None):
        selection = selection or self.selection()
        return {'operation': 'extract_material', 'paper_selection': selection,
                'citation_mode': CITATION_MODE, 'material_span_catalog': build_span_catalog(selection)}

    def test_unicode_offsets_stable_ids_and_complete_nonspace_coverage(self):
        selection = self.selection(chunk_chars=64)
        before = deepcopy(selection)
        catalog = build_span_catalog(selection, max_span_chars=64)
        self.assertEqual(catalog, build_span_catalog(selection, max_span_chars=64))
        self.assertEqual(selection, before)
        self.assertNotIn('UNSELECTED_SENTINEL', str(catalog))
        chunks = {item['id']: item for item in selection['chunks']}
        covered = {identity: set() for identity in chunks}
        for span in catalog['spans']:
            chunk = chunks[span['chunk_id']]
            start, end = span['normalized_start'] - chunk['start'], span['normalized_end'] - chunk['start']
            self.assertEqual(span['quote'], chunk['text'][start:end])
            self.assertEqual(span['page'], chunk['page'])
            self.assertEqual(span['text_digest'], hashlib.sha256(span['quote'].encode()).hexdigest())
            self.assertLessEqual(len(span['quote']), 64)
            self.assertTrue(0 <= start < end <= len(chunk['text']))
            self.assertTrue(covered[chunk['id']].isdisjoint(range(start, end)))
            covered[chunk['id']].update(range(start, end))
        for identity, chunk in chunks.items():
            self.assertTrue({i for i, char in enumerate(chunk['text']) if not char.isspace()} <= covered[identity])
        first = catalog['spans'][0]
        self.assertIn('😀', first['quote'])
        self.assertNotEqual(len(first['quote']), len(first['quote'].encode()))
        self.assertEqual(validate_span_catalog(selection, catalog), catalog)

    def test_duplicate_text_positions_pages_and_catalogue_bindings_are_distinct(self):
        selection = self.selection(['same text ' * 10, 'same text ' * 10], [1, 2], chunk_chars=64)
        catalog = build_span_catalog(selection, max_span_chars=64)
        self.assertEqual(len(catalog['spans']), len({span['id'] for span in catalog['spans']}))
        other = self.selection(['same text ' * 10, 'same text ' * 10, 'new page'], [1, 2], chunk_chars=64)
        other_catalog = build_span_catalog(other, max_span_chars=64)
        self.assertTrue({x['id'] for x in catalog['spans']}.isdisjoint(x['id'] for x in other_catalog['spans']))
        with self.assertRaises(MaterialError):
            validate_span_catalog(other, catalog)

    def test_long_words_or_formulas_have_explicit_character_fallback(self):
        selection = self.selection(['x' * 130 + ' words after'], chunk_chars=16000)
        catalog = build_span_catalog(selection, max_span_chars=64)
        self.assertEqual(catalog['spans'][0]['boundary_kind'], 'character_fallback')
        self.assertEqual(catalog['spans'][1]['boundary_kind'], 'character_fallback')
        self.assertEqual(catalog['spans'][0]['quote'], 'x' * 64)
        normal = build_span_catalog(self.selection(['hello ' * 30]), max_span_chars=64)
        self.assertEqual(normal['spans'][0]['boundary_kind'], 'word_boundary')
        self.assertFalse(normal['spans'][0]['quote'].endswith(' '))

    def test_rehashed_tampering_omission_duplicate_order_extra_or_offsets_fail(self):
        selection = self.selection(chunk_chars=64)
        catalog = build_span_catalog(selection, max_span_chars=64)
        changes = []
        for key, value in [('page', 2), ('quote', 'tampered'), ('normalized_start', 1), ('text_digest', 'f' * 64), ('id', 'span_' + 'e' * 56)]:
            item = deepcopy(catalog); item['spans'][0][key] = value; changes.append(item)
        item = deepcopy(catalog); item['spans'].pop(); changes.append(item)
        item = deepcopy(catalog); item['spans'] = item['spans'][::-1]; changes.append(item)
        item = deepcopy(catalog); item['spans'].append(deepcopy(item['spans'][0])); changes.append(item)
        item = deepcopy(catalog); item['authority'] = True; changes.append(item)
        for item in changes:
            item['catalog_digest'] = digest({k: v for k, v in item.items() if k != 'catalog_digest'})
            with self.subTest(item=list(item)), self.assertRaises(MaterialError):
                validate_span_catalog(selection, item)

    def test_mode_is_explicit_complete_and_extraction_only(self):
        context = self.context()
        self.assertTrue(validate_context_mode(context))
        self.assertFalse(validate_context_mode({'operation': 'generate_candidates'}))
        for change in ({'citation_mode': 'legacy'}, {'citation_mode': None}, {'operation': 'generate_candidates'}):
            item = deepcopy(context); item.update(change)
            with self.subTest(change=change), self.assertRaises(MaterialError):
                validate_context_mode(item)
        for key in ('citation_mode', 'material_span_catalog'):
            item = deepcopy(context); del item[key]
            with self.assertRaises(MaterialError):
                validate_context_mode(item)
        with self.assertRaises(MaterialError):
            validate_context_mode(context, 'generate_candidates')
        item = deepcopy(context); del item['operation']
        with self.assertRaises(MaterialError):
            validate_context_mode(item)
        self.assertTrue(validate_context_mode(item, 'extract_material'))

    def test_invalid_bounds_count_and_utf8_byte_budget_reject_without_truncation(self):
        selection = self.selection()
        for bound in (True, 63, 601, 64.0):
            with self.subTest(bound=bound), self.assertRaises(MaterialError):
                build_span_catalog(selection, max_span_chars=bound)
        with self.assertRaises(MaterialError) as caught:
            build_span_catalog(self.selection(['x' * 33000], chunk_chars=16000), max_span_chars=64)
        self.assertEqual(caught.exception.code, 'MATERIAL_SPAN_CATALOG_TOO_LARGE')
        with self.assertRaises(MaterialError) as caught:
            build_span_catalog(self.selection(['中' * 20000], chunk_chars=16000), max_span_chars=64)
        self.assertEqual(caught.exception.code, 'MATERIAL_SPAN_CATALOG_TOO_LARGE')


if __name__ == '__main__':
    unittest.main()
