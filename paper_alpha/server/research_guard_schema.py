"""Bounded, closed temporal reservation and development-exposure projections."""
from typing import Literal
from pydantic import Field
from ..author_panel_schema import AuthorModel


class GuardInterval(AuthorModel):
    start: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    end: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')


class GuardBoundary(AuthorModel):
    revision_id: str
    research_id: str
    dataset_id: str
    test: GuardInterval
    development: GuardInterval
    origin: Literal['legacy', 'created']
    task_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    submitted_runs: int = Field(ge=0)
    started_runs: int = Field(ge=0)


class GuardConflict(AuthorModel):
    revision_id: str
    reserved_by_revision_id: str
    overlap: GuardInterval


class GuardCheck(AuthorModel):
    data_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    allowed: bool
    reason: str | None = Field(max_length=4000)
    development: GuardInterval
    test: GuardInterval


class GuardStatus(AuthorModel):
    research_id: str
    dataset_id: str
    data_sha256: str | None = Field(pattern=r'^[0-9a-f]{64}$')
    status: Literal['protected', 'historical_conflict', 'unresolved']
    reason: str | None = Field(max_length=4000)
    boundaries: list[GuardBoundary] = Field(max_length=1000)
    conflicts: list[GuardConflict] = Field(max_length=1000)
    total_boundaries: int = Field(ge=0)
    omitted_boundaries: int = Field(ge=0)
    total_conflicts: int = Field(ge=0)
    omitted_conflicts: int = Field(ge=0)
    limitations: list[str] = Field(max_length=20)


class GuardPage(AuthorModel):
    items: list[GuardStatus] = Field(max_length=100)
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
