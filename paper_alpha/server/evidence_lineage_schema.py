"""Bounded public source lineage; identity proof is not scientific approval."""
from typing import Literal
from pydantic import Field

from ..author_panel_schema import AuthorModel
from .paper_materials_schema import AcceptedMaterialHypothesis, MaterialHypothesis
from .research_jobs_schema import DraftEvidence, DraftHypothesis

SHA = r'^[0-9a-f]{64}$'
ChangeStatus = Literal['unchanged', 'edited', 'added', 'unknown']


class LineageTarget(AuthorModel):
    run_id: str
    revision_id: str
    candidate_id: str
    attempt_id: str
    state_digest: str = Field(pattern=SHA)
    result_digest: str = Field(pattern=SHA)


class LineageProposal(AuthorModel):
    proposal_id: str
    attempt_id: str
    context_digest: str = Field(pattern=SHA)
    accepted_response_digest: str = Field(pattern=SHA)


class LineageMaterial(AuthorModel):
    id: str
    version_id: str
    draft_digest: str = Field(pattern=SHA)
    material_digest: str = Field(pattern=SHA)


class LineageSelection(AuthorModel):
    paper_id: str
    paper_sha256: str = Field(pattern=SHA)
    document_digest: str = Field(pattern=SHA)
    selection_digest: str = Field(pattern=SHA)
    normalization_version: Literal['nfkc-whitespace-unicode-v1']
    offset_basis: Literal['normalized_pdf_page_unicode_code_points_half_open']
    printed_page_verification: Literal['unverified']


class LineageProvenance(AuthorModel):
    experiment_id: str
    generation: LineageProposal
    material: LineageMaterial
    extraction: LineageProposal
    selection: LineageSelection


class LineageQuote(AuthorModel):
    id: str
    page: int = Field(ge=1, le=500)
    quote: str = Field(min_length=1, max_length=4096)
    chunk_id: str
    normalized_start: int = Field(ge=0, le=2000000)
    normalized_end: int = Field(ge=1, le=2000000)
    printed_page: str | None
    original_quote_sha256: str = Field(pattern=SHA)


class LineageExcerpt(AuthorModel):
    source_stage: Literal['adopted']
    before: str = Field(max_length=256)
    quote: str = Field(min_length=1, max_length=4096)
    after: str = Field(max_length=256)
    bounded_context: Literal[True]
    exact_highlight: bool
    public_projection_redacted: bool


class LineageEvidence(AuthorModel):
    id: str
    status: ChangeStatus
    reason_code: str
    inheritance_intact: bool
    original: LineageQuote | None
    adopted: LineageQuote | None
    final: DraftEvidence
    excerpt: LineageExcerpt | None


class LineageHypothesis(AuthorModel):
    id: str
    status: ChangeStatus
    inheritance_intact: bool
    original: MaterialHypothesis | None
    adopted: AcceptedMaterialHypothesis | None
    final: DraftHypothesis | None
    changes: list[str] = Field(max_length=30)


class LineageHistory(AuthorModel):
    version_id: str
    digest: str = Field(pattern=SHA)
    changed_paths: list[str] = Field(max_length=256)


class EvidenceLineageResponse(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['run_evidence_lineage']
    target: LineageTarget
    status: Literal['verified', 'unknown']
    reason_code: str
    provenance: LineageProvenance | None
    hypothesis: LineageHypothesis
    evidence: list[LineageEvidence] = Field(max_length=100)
    history: list[LineageHistory] = Field(max_length=100)
    semantic_fidelity: Literal['unverified']
    llm_api_called: Literal[False]
    public_projection_redacted: bool
    public_view_digest: str = Field(pattern=SHA)
