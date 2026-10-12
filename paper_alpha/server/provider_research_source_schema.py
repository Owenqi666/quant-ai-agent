"""Closed source descriptors shared by v2 proposals and budget sessions."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel

class MaterialDestination(AuthorModel):
    kind: Literal['new_research', 'existing_research']
    research_id: str | None = None
    base_revision_id: str | None = None

    @model_validator(mode='after')
    def destination(self):
        if self.kind == 'new_research':
            if self.research_id is not None or self.base_revision_id is not None:
                raise ValueError('New research has no prior revision')
        elif not self.research_id or not self.base_revision_id:
            raise ValueError('Existing research requires an explicit base revision')
        return self


class PaperSelectionSource(AuthorModel):
    kind: Literal['paper_selection']
    paper_id: str = Field(min_length=1, max_length=64)
    selected_pages: list[Annotated[int, Field(ge=1, le=500)]] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def ordered_pages(self):
        if self.selected_pages != sorted(set(self.selected_pages)):
            raise ValueError('Selected physical pages must be unique and increasing')
        return self


class AcceptedMaterialSource(AuthorModel):
    kind: Literal['accepted_material']
    material_id: str = Field(min_length=1, max_length=128)
    version_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=64)
    evaluation: dict
    method_policy: dict
    destination: MaterialDestination
    title: str = Field(min_length=1, max_length=200)


class ResearchRevisionSource(AuthorModel):
    kind: Literal['research_revision']
    research_id: str = Field(min_length=1, max_length=64)
    base_revision_id: str = Field(min_length=1, max_length=64)


ResearchSource = Annotated[PaperSelectionSource | AcceptedMaterialSource | ResearchRevisionSource,
                           Field(discriminator='kind')]

