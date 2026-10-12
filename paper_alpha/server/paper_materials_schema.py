"""Closed paper-material data contracts, separate from executable research drafts."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator

from ..author_panel_schema import AuthorModel

ID = r'^[A-Za-z0-9_-]{1,64}$'
DIGEST = r'^[a-f0-9]{64}$'
MaterialAttribution = Literal['paper_original', 'model_conjecture', 'unresolved']
AcceptedAttribution = Literal['paper_original', 'model_conjecture', 'unresolved', 'user_modification']


class MaterialEvidence(AuthorModel):
    id: str = Field(pattern=ID)
    page: int = Field(ge=1, le=500)
    chunk_id: str = Field(min_length=1, max_length=96)
    quote: str = Field(min_length=1, max_length=4096)
    normalized_start: int = Field(ge=0, le=2_000_000)
    normalized_end: int = Field(ge=1, le=2_000_000)
    printed_page: str | None = Field(max_length=80)

    @model_validator(mode='after')
    def span(self):
        if self.normalized_end <= self.normalized_start or not self.quote.strip():
            raise ValueError('Evidence requires non-empty quote and increasing Unicode span')
        return self


class MaterialData(AuthorModel):
    name: str = Field(min_length=1, max_length=100)
    semantics: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(max_length=30)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.name.strip() or not self.semantics.strip():
            raise ValueError('Data name and semantics must not be blank')
        return self


class MaterialHypothesis(AuthorModel):
    id: str = Field(pattern=ID)
    claim: str = Field(min_length=1, max_length=4000)
    claim_attribution: MaterialAttribution
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    economic_mechanism: str | None = Field(max_length=4000)
    mechanism_attribution: MaterialAttribution
    signal_direction: str | None = Field(max_length=2000)
    required_data: list[MaterialData] = Field(max_length=30)
    calculation: str | None = Field(max_length=4000)
    conditions: list[str] = Field(max_length=30)
    unresolved_rules: list[str] = Field(max_length=30)

    @model_validator(mode='after')
    def shape(self):
        if not self.claim.strip():
            raise ValueError('Claim may not be blank')
        if self.economic_mechanism is None and self.mechanism_attribution != 'unresolved':
            raise ValueError('Unknown mechanism requires unresolved attribution')
        for value in (self.economic_mechanism, self.signal_direction, self.calculation):
            if value is not None and not value.strip():
                raise ValueError('Use null for unknown content, not blank text')
        for values in (self.conditions, self.unresolved_rules):
            if any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values):
                raise ValueError('Material rules must be bounded nonblank text')
        if len({item.name for item in self.required_data}) != len(self.required_data):
            raise ValueError('Data semantic names must be unique')
        return self


class AcceptedMaterialHypothesis(MaterialHypothesis):
    claim_attribution: AcceptedAttribution
    mechanism_attribution: AcceptedAttribution


class MaterialDraft(AuthorModel):
    title: str = Field(min_length=1, max_length=200)
    evidence: list[MaterialEvidence] = Field(min_length=1, max_length=30)
    hypotheses: list[MaterialHypothesis] = Field(min_length=1, max_length=10)
    unresolved_rules: list[str] = Field(max_length=30)
    limitations: list[str] = Field(max_length=30)

    @model_validator(mode='after')
    def links(self):
        if not self.title.strip():
            raise ValueError('Material title must not be blank')
        evidence = {item.id for item in self.evidence}
        if len(evidence) != len(self.evidence) or len({item.id for item in self.hypotheses}) != len(self.hypotheses):
            raise ValueError('Material evidence/hypothesis IDs must be unique')
        for hypothesis in self.hypotheses:
            for ids in [hypothesis.evidence_ids, *[data.evidence_ids for data in hypothesis.required_data]]:
                if len(ids) != len(set(ids)) or not set(ids) <= evidence:
                    raise ValueError('Material references unknown or duplicate evidence')
        for values in (self.unresolved_rules, self.limitations):
            if any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in values):
                raise ValueError('Material rules must be bounded nonblank text')
        return self


class AcceptedMaterialDraft(MaterialDraft):
    hypotheses: list[AcceptedMaterialHypothesis] = Field(min_length=1, max_length=10)


class AcceptedMaterialPacket(AuthorModel):
    """Code-owned authoritative ledger projection, never a browser/model input."""
    contract_id: Literal['research_provider_v2']
    operation: Literal['extract_material']
    proposal_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    accepted_response_digest: str = Field(pattern=DIGEST)
    selection_digest: str = Field(pattern=DIGEST)
    material_digest: str = Field(pattern=DIGEST)


class PaperChunk(AuthorModel):
    id: str = Field(min_length=1, max_length=96)
    page: int = Field(ge=1, le=500)
    start: int = Field(ge=0, le=2_000_000)
    end: int = Field(ge=1, le=2_000_000)
    text: str = Field(min_length=1, max_length=16000)
    text_digest: str = Field(pattern=DIGEST)


class PaperSelection(AuthorModel):
    schema_version: Literal[1]
    normalization_version: Literal['nfkc-whitespace-unicode-v1']
    paper_id: str = Field(min_length=1, max_length=64)
    paper_sha256: str = Field(pattern=DIGEST)
    document_digest: str = Field(pattern=DIGEST)
    extractor: str = Field(min_length=1, max_length=300)
    document_pages: int = Field(ge=1, le=500)
    chunk_chars: int = Field(ge=64, le=16000)
    selected_pages: list[int] = Field(min_length=1, max_length=20)
    omitted_pages: list[int] = Field(max_length=500)
    chunks: list[PaperChunk] = Field(min_length=1, max_length=200)
    selection_digest: str = Field(pattern=DIGEST)
