"""Deterministic short citations over a verified, frozen selected-page snapshot.

Coordinates are normalized Unicode characters, never UTF-8 bytes. Spans stay
inside their existing chunk. This catalogue carries text facts and no semantic
judgment; a boundary may cut an overlong word/formula and is marked explicitly.
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel
from ..evidence import normalize
from ..storage import digest, json_text
from .paper_segments import MaterialError, validate_selection

CITATION_MODE = 'material_span_ids_v1'
CATALOG_VERSION = 'paper-material-short-spans-v1'
MAX_SPAN_CHARS = 600
MAX_SPANS = 512
MAX_CATALOG_BYTES = 131072
SPAN_ID = r'^span_[a-f0-9]{56}$'
SHA = r'^[a-f0-9]{64}$'


class MaterialSpan(AuthorModel):
    id: str = Field(pattern=SPAN_ID)
    page: int = Field(ge=1, le=500)
    chunk_id: str = Field(min_length=1, max_length=96)
    normalized_start: int = Field(ge=0, le=2_000_000)
    normalized_end: int = Field(ge=1, le=2_000_000)
    quote: str = Field(min_length=1, max_length=MAX_SPAN_CHARS)
    text_digest: str = Field(pattern=SHA)
    boundary_kind: Literal['word_boundary', 'chunk_end', 'character_fallback']

    @model_validator(mode='after')
    def increasing(self):
        if self.normalized_end <= self.normalized_start or not self.quote.strip():
            raise ValueError('A material span requires increasing coordinates and nonblank text')
        return self


class MaterialSpanCatalog(AuthorModel):
    schema_version: Literal[1]
    catalog_version: Literal['paper-material-short-spans-v1']
    normalization_version: Literal['nfkc-whitespace-unicode-v1']
    selection_digest: str = Field(pattern=SHA)
    max_span_chars: int = Field(ge=64, le=MAX_SPAN_CHARS)
    spans: list[MaterialSpan] = Field(min_length=1, max_length=MAX_SPANS)
    catalog_digest: str = Field(pattern=SHA)

    @model_validator(mode='after')
    def unique(self):
        if len({span.id for span in self.spans}) != len(self.spans):
            raise ValueError('Catalogue span IDs must be unique')
        return self


def catalog_schema() -> dict:
    return MaterialSpanCatalog.model_json_schema()


def _error(message, code='MATERIAL_SPAN_CATALOG_INVALID', status=422):
    return MaterialError(message, status, code=code)


def _selection(selection):
    if (not isinstance(selection, dict) or not isinstance(selection.get('chunks'), list)
            or not 1 <= len(selection['chunks']) <= 200
            or any(not isinstance(chunk, dict) or not isinstance(chunk.get('text'), str)
                   or len(chunk['text']) > 16000 for chunk in selection['chunks'])):
        raise _error('Material spans require a bounded selected-page snapshot')
    return validate_selection(selection)


def build_span_catalog(selection: dict, *, max_span_chars: int = MAX_SPAN_CHARS) -> dict:
    """Build the complete bounded directory, rejecting rather than truncating.

    A whitespace gap between spans is excluded. All non-whitespace characters
    in every sent chunk are included once. Prefer a word boundary, but explicitly
    mark a character cut when one word/formula exceeds the character budget.
    """
    if type(max_span_chars) is not int or not 64 <= max_span_chars <= MAX_SPAN_CHARS:
        raise _error('Span size must be an integer between 64 and 600 characters')
    selection = _selection(selection)
    spans = []
    for chunk in selection['chunks']:
        text = chunk['text']
        if normalize(text) != text.strip():
            raise _error('Chunk text differs from its declared normalization')
        cursor = 0
        while cursor < len(text):
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor == len(text):
                break
            end = min(cursor + max_span_chars, len(text))
            kind = 'chunk_end'
            if end < len(text):
                boundary = text.rfind(' ', cursor + 1, end + 1)
                if boundary > cursor:
                    end, kind = boundary, 'word_boundary'
                else:
                    kind = 'character_fallback'
            while end > cursor and text[end - 1].isspace():
                end -= 1
            quote = text[cursor:end]
            start, stop = chunk['start'] + cursor, chunk['start'] + end
            text_digest = hashlib.sha256(quote.encode('utf-8')).hexdigest()
            binding = {'selection_digest': selection['selection_digest'], 'page': chunk['page'],
                       'chunk_id': chunk['id'], 'normalized_start': start,
                       'normalized_end': stop, 'text_digest': text_digest}
            spans.append({'id': 'span_' + digest(binding)[:56], 'page': chunk['page'],
                          'chunk_id': chunk['id'], 'normalized_start': start,
                          'normalized_end': stop, 'quote': quote, 'text_digest': text_digest,
                          'boundary_kind': kind})
            if len(spans) > MAX_SPANS:
                raise _error('Complete span directory exceeds the count budget; nothing was truncated',
                             'MATERIAL_SPAN_CATALOG_TOO_LARGE', 413)
            cursor = end
    body = {'schema_version': 1, 'catalog_version': CATALOG_VERSION,
            'normalization_version': selection['normalization_version'],
            'selection_digest': selection['selection_digest'], 'max_span_chars': max_span_chars,
            'spans': spans}
    body['catalog_digest'] = digest(body)
    if len(json_text(body).encode('utf-8')) > MAX_CATALOG_BYTES:
        raise _error('Complete span directory exceeds the byte budget; nothing was truncated',
                     'MATERIAL_SPAN_CATALOG_TOO_LARGE', 413)
    return MaterialSpanCatalog.model_validate(body).model_dump()


def validate_span_catalog(selection: dict, catalog: dict) -> dict:
    """Recreate every coordinate, text digest, ID and complete directory exactly.

    A caller can recompute a digest; it cannot replace source spans, omit entries,
    reorder entries or use a different selection as this catalogue.
    """
    if (not isinstance(catalog, dict) or not isinstance(catalog.get('spans'), list)
            or not 1 <= len(catalog['spans']) <= MAX_SPANS):
        raise _error('Material span directory is missing or exceeds its count bound')
    try:
        clean = MaterialSpanCatalog.model_validate(catalog).model_dump()
    except (ValueError, TypeError) as exc:
        raise _error('Material span directory does not match its closed schema') from exc
    if len(json_text(clean).encode('utf-8')) > MAX_CATALOG_BYTES:
        raise _error('Material span directory exceeds its byte budget', 'MATERIAL_SPAN_CATALOG_TOO_LARGE', 413)
    expected = build_span_catalog(selection, max_span_chars=clean['max_span_chars'])
    if clean != expected:
        raise _error('Material span directory differs from the complete frozen source')
    return clean


def validate_context_mode(context: dict, operation: str | None = None) -> bool:
    """Legacy mode requires both new keys absent; malformed modes never downgrade."""
    if not isinstance(context, dict):
        raise _error('Material citation context must be an object', 'MATERIAL_SPAN_MODE_INVALID')
    has_mode, has_catalog = 'citation_mode' in context, 'material_span_catalog' in context
    if not has_mode and not has_catalog:
        return False
    operation = context.get('operation') if operation is None else operation
    if (not has_mode or not has_catalog or context['citation_mode'] != CITATION_MODE
            or operation != 'extract_material'
            or 'operation' in context and context['operation'] != operation):
        raise _error('Short-span mode is explicit, complete and extraction-only', 'MATERIAL_SPAN_MODE_INVALID')
    validate_span_catalog(context.get('paper_selection'), context['material_span_catalog'])
    return True
