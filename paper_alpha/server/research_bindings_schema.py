"""Narrow author-source preparation contracts; never execution authority."""
from typing import Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel, AuthorSource
from ..eligibility_schema import EligibilityPlan, EligibilitySummary
from .research_protocol_schema import ProtocolConfig

SHA = r'^[0-9a-f]{64}$'


class BindingPanelRef(AuthorModel):
    id: str = Field(pattern=r'^author_panel_[0-9a-f]{64}$')
    digest: str = Field(pattern=SHA)


class BindingPrepareRequest(AuthorModel):
    paper_id: str = Field(min_length=1, max_length=64)
    protocol_id: str = Field(pattern=r'^protocol_[0-9a-f]{64}$')
    study_id: str = Field(pattern=r'^author_study_[0-9a-f]{64}$')
    source_scope: Literal['author_paper', 'controlled_contract_fixture']
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(default='', max_length=4000)


class BindingPreviewRequest(AuthorModel):
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(default='', max_length=4000)
    source_scope: Literal['author_paper', 'controlled_contract_fixture']
    paper_id: str = Field(min_length=1, max_length=64)
    paper_digest: str = Field(pattern=SHA)
    protocol_id: str = Field(pattern=r'^protocol_[0-9a-f]{64}$')
    protocol_digest: str = Field(pattern=SHA)
    study_id: str = Field(pattern=r'^author_study_[0-9a-f]{64}$')
    study_digest: str = Field(pattern=SHA)
    source_filename: Literal['IntnlData.mat', 'USData.mat']
    source_sha256: str = Field(pattern=SHA)
    plan_digest: str = Field(pattern=SHA)
    scan_digest: str = Field(pattern=SHA)
    result_digest: str = Field(pattern=SHA)
    panels: list[BindingPanelRef] = Field(default_factory=list, max_length=8)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.title.strip() or not self.paper_id.strip():
            raise ValueError('Binding title and paper identity must be nonblank')
        if len({p.id for p in self.panels}) != len(self.panels):
            raise ValueError('Panel references must be unique')
        return self


class BindingCreate(BindingPreviewRequest):
    preview_digest: str = Field(pattern=SHA)
    idempotency_key: str = Field(min_length=1, max_length=128)


class BindingEvidence(AuthorModel):
    id: str
    origin: Literal['paper', 'author_code', 'project']
    locator: str
    claim: str
    verification: Literal['literal_quote_verified', 'source_registry_reference', 'project_declaration']
    document_sha256: str | None


class BindingPaper(AuthorModel):
    id: str
    title: str
    pdf_sha256: str
    document_digest: str
    evidence_scope: Literal['author_paper_literal_anchors', 'unrelated_fixture_document_contract']


class BindingProtocol(AuthorModel):
    id: str
    digest: str
    config_digest: str
    config: ProtocolConfig
    unresolved: list[str]


class BindingStudy(AuthorModel):
    id: str
    digest: str
    plan: EligibilityPlan
    plan_digest: str
    scan_digest: str
    result_digest: str
    summary: EligibilitySummary
    evidence_digest: str
    panels: list[BindingPanelRef]


class BindingContext(AuthorModel):
    schema_version: Literal[1]
    semantics_version: Literal['gjs-research-binding-v1']
    source_scope: Literal['author_paper', 'controlled_contract_fixture']
    paper: BindingPaper
    protocol: BindingProtocol
    source: AuthorSource
    study: BindingStudy
    evidence: list[BindingEvidence]
    compatible: Literal[True]
    status: Literal['blocked']
    blockers: list[str]
    window_rules: dict[str, str | int]
    verification_scope: Literal['linked_resource_consistency_only']
    raw_source_reverified: Literal[False]
    execution_ready: Literal[False]
    provider_connected: Literal[False]
    semantic_fidelity: Literal['unverified']
    limitations: list[str]


class BindingPreview(AuthorModel):
    request: BindingPreviewRequest
    preview_digest: str
    context: BindingContext


class ResearchBinding(AuthorModel):
    id: str
    digest: str
    created_at: str
    request: BindingPreviewRequest
    preview_digest: str
    context: BindingContext


class BindingSummary(AuthorModel):
    id: str
    digest: str
    created_at: str
    title: str
    source_scope: Literal['author_paper', 'controlled_contract_fixture']
    paper_id: str
    protocol_id: str
    study_id: str
    source_filename: str
    status: Literal['blocked']
    blockers: list[str]
    raw_source_reverified: Literal[False]
    execution_ready: Literal[False]


class BindingPage(AuthorModel):
    items: list[BindingSummary]
    total: int
    limit: int
    offset: int


class BindingExport(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['research_binding_export']
    binding: ResearchBinding
    binding_digest: str = Field(pattern=SHA)
    scope: Literal['Exact source-linked blocked preparation; no raw MAT authentication or portfolio authority.']
