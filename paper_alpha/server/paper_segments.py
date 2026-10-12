"""Deterministic selected-page text with exact normalized Unicode-span citations."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pypdf

from ..evidence import normalize, verify_paper
from ..storage import digest, json_text
from .paper_materials_schema import AcceptedMaterialDraft, MaterialDraft, PaperSelection
from .service import ServiceError

NORMALIZATION_VERSION = 'nfkc-whitespace-unicode-v1'
MAX_SELECTION_BYTES = 98304


class MaterialError(ServiceError):
    def __init__(self, message, status=409, *, code='MATERIAL_INVALID'):
        super().__init__(message, status)
        self.code = code
        self.retryable = False


def _text_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def segment_document(document, selected_pages, *, max_context_bytes=MAX_SELECTION_BYTES, chunk_chars=4000):
    """Pure projection. No paths, generated candidates, labels or other paper pages."""
    if type(chunk_chars) is not int or not 64 <= chunk_chars <= 16000 or type(max_context_bytes) is not int or not 1024 <= max_context_bytes <= 131072:
        raise MaterialError('Invalid text selection bounds', 422)
    if not isinstance(selected_pages, list) or not 1 <= len(selected_pages) <= 20 or any(type(page) is not int for page in selected_pages) or selected_pages != sorted(set(selected_pages)):
        raise MaterialError('Select 1..20 unique physical pages in ascending order', 422, code='PAGE_SELECTION_INVALID')
    pages = document.get('pages', [])
    if not isinstance(pages, list) or not 1 <= len(pages) <= 500 or [item.get('page') for item in pages] != list(range(1, len(pages) + 1)) or any(not isinstance(item.get('text'), str) for item in pages):
        raise MaterialError('Extracted document pages are malformed', code='PAPER_TEXT_INVALID')
    if sum(len(item['text']) for item in pages) > 2_000_000 or not set(selected_pages) <= set(range(1, len(pages) + 1)):
        raise MaterialError('Selected pages are unavailable or document exceeds bound', 422, code='PAGE_SELECTION_INVALID')
    chunks = []
    for page in selected_pages:
        text = normalize(pages[page - 1]['text'])
        if not text:
            raise MaterialError('Selected page has no reliable extracted text; supplement the material', 422, code='PAGE_TEXT_UNAVAILABLE')
        for start in range(0, len(text), chunk_chars):
            chunk = text[start:start + chunk_chars]
            chunks.append({'id': f'page-{page}-chars-{start}-{start + len(chunk)}', 'page': page,
                           'start': start, 'end': start + len(chunk), 'text': chunk, 'text_digest': _text_digest(chunk)})
    body = {'schema_version': 1, 'normalization_version': NORMALIZATION_VERSION,
            'paper_id': document['id'], 'paper_sha256': document['document_sha256'],
            'document_digest': digest(document),
            'extractor': f'pypdf/{pypdf.__version__}; {document["extraction"]}',
            'document_pages': len(pages), 'chunk_chars': chunk_chars, 'selected_pages': deepcopy(selected_pages),
            'omitted_pages': [page for page in range(1, len(pages) + 1) if page not in selected_pages], 'chunks': chunks}
    clean = PaperSelection.model_validate({**body, 'selection_digest': digest(body)}).model_dump()
    if len(json_text(clean).encode('utf-8')) > max_context_bytes:
        raise MaterialError('Complete selected pages exceed context budget; select fewer pages', 413, code='CONTEXT_TOO_LARGE')
    return clean


def validate_selection(selection):
    clean = PaperSelection.model_validate(selection).model_dump()
    if clean['selection_digest'] != digest({key: value for key, value in clean.items() if key != 'selection_digest'}):
        raise MaterialError('Selection digest differs', code='SOURCE_INTEGRITY')
    selected = clean['selected_pages']
    if any(type(page) is not int for page in selected) or selected != sorted(set(selected)) or not set(selected) <= set(range(1, clean['document_pages'] + 1)):
        raise MaterialError('Invalid selected-page manifest', code='SOURCE_INTEGRITY')
    if clean['omitted_pages'] != [page for page in range(1, clean['document_pages'] + 1) if page not in selected]:
        raise MaterialError('Omitted-page manifest differs', code='SOURCE_INTEGRITY')
    if [item['page'] for item in clean['chunks']] != sorted(item['page'] for item in clean['chunks']) or len({item['id'] for item in clean['chunks']}) != len(clean['chunks']):
        raise MaterialError('Invalid chunk ordering or duplicate identities', code='SOURCE_INTEGRITY')
    for page in selected:
        chunks = [item for item in clean['chunks'] if item['page'] == page]
        if not chunks:
            raise MaterialError('Selected page has no chunks', code='SOURCE_INTEGRITY')
        start = 0
        for index, item in enumerate(chunks):
            if (item['start'] != start or item['end'] != start + len(item['text'])
                    or len(item['text']) > clean['chunk_chars'] or (index < len(chunks) - 1 and len(item['text']) != clean['chunk_chars'])
                    or item['text_digest'] != _text_digest(item['text'])
                    or item['id'] != f'page-{page}-chars-{item["start"]}-{item["end"]}'):
                raise MaterialError('Chunk bytes/span/digest differs', code='SOURCE_INTEGRITY')
            start = item['end']
    if any(item['page'] not in selected for item in clean['chunks']):
        raise MaterialError('Unselected page present in context', code='SOURCE_INTEGRITY')
    return clean


def build_paper_selection(store, paper_id, selected_pages, *, max_context_bytes=MAX_SELECTION_BYTES, chunk_chars=4000):
    row = store._fetch('papers', paper_id)
    document = json.loads(row['document'])
    if document.get('id') != paper_id or row['sha256'] != document.get('document_sha256'):
        raise MaterialError('Paper row identity differs', code='SOURCE_INTEGRITY')
    try:
        verify_paper(document, store.paper_path(paper_id))
    except (OSError, ValueError) as exc:
        raise MaterialError('Paper PDF or extracted text changed', code='SOURCE_CHANGED') from exc
    selection = segment_document(document, selected_pages, max_context_bytes=max_context_bytes, chunk_chars=chunk_chars)
    if store._fetch('papers', paper_id) != row:
        raise MaterialError('Paper changed during selection', code='SOURCE_CHANGED')
    return selection


def verify_current_selection(store, selection):
    selection = validate_selection(selection)
    current = build_paper_selection(store, selection['paper_id'], selection['selected_pages'],
                                    max_context_bytes=131072, chunk_chars=selection['chunk_chars'])
    if current != selection:
        raise MaterialError('Frozen paper/text selection is stale', code='SOURCE_CHANGED')
    return selection


def validate_material_draft(selection, draft, *, accepted=False):
    selection = validate_selection(selection)
    if isinstance(draft, dict) and draft.get('grounding_version') == 1:
        from .grounded_material_schema import material_model
        model = material_model(draft, accepted=accepted)
    else:
        model = AcceptedMaterialDraft if accepted else MaterialDraft
    value = model.model_validate(draft).model_dump()
    chunks = {item['id']: item for item in selection['chunks']}
    for evidence in value['evidence']:
        chunk = chunks.get(evidence['chunk_id'])
        start, end = evidence['normalized_start'], evidence['normalized_end']
        quote = normalize(evidence['quote'])
        if chunk is None or evidence['page'] != chunk['page'] or not chunk['start'] <= start < end <= chunk['end'] or chunk['text'][start - chunk['start']:end - chunk['start']] != quote:
            raise MaterialError('Evidence must match its sent chunk, physical page and exact Unicode span', 422, code='EVIDENCE_SPAN_MISMATCH')
    if len(json_text(value).encode('utf-8')) > 65536:
        raise MaterialError('Material output exceeds 64 KiB', 413, code='MATERIAL_TOO_LARGE')
    return {'valid': True, 'material': value, 'citation_integrity': 'verified', 'semantic_fidelity': 'unverified',
            'normalization_version': NORMALIZATION_VERSION,
            'printed_page_verification': 'unverified', 'diagnostics': []}
