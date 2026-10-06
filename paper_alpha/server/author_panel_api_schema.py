"""Closed HTTP contracts for bounded author-panel imports and diagnostics."""
from typing import Literal

from pydantic import Field, model_validator

from ..author_panel_schema import (AuthorModel, AuthorPanel, AuthorSource, AuthorSelection,
                                   AuthorDiagnostic, AuthorReference, AuthorSummary)


class AuthorPanelCreate(AuthorModel):
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(max_length=4000)
    panel: AuthorPanel
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode='before')
    @classmethod
    def validate_raw_panel(cls, value):
        # Literal[1] otherwise accepts True/1.0 before scientific validation.
        if isinstance(value, dict) and 'panel' in value:
            from ..author_panel import validate_panel
            validate_panel(value['panel'])
        return value


class AuthorPanelDetail(AuthorModel):
    id: str
    title: str
    note: str
    created_at: str
    digest: str
    panel: AuthorPanel
    result: AuthorDiagnostic
    reference: AuthorReference
    verification_scope: Literal['normalized_panel_and_diagnostics']
    raw_source_reverified: Literal[False]


class AuthorPanelSummary(AuthorModel):
    id: str
    title: str
    note: str
    created_at: str
    digest: str
    source: AuthorSource
    selection: AuthorSelection
    summary: AuthorSummary
    verification_scope: Literal['normalized_panel_and_diagnostics']
    raw_source_reverified: Literal[False]


class AuthorPanelPage(AuthorModel):
    items: list[AuthorPanelSummary]
    total: int
    limit: int
    offset: int
