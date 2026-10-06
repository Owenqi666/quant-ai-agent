"""Closed, bounded projection of one immutable candidate evaluation."""
from typing import Literal

from pydantic import Field

from .response_schemas import ResponseModel


MAX_POINTS = 4000


class CandidateSeriesSource(ResponseModel):
    artifact_id: str
    artifact_name: str
    artifact_sha256: str
    original_sha256: str
    result_sha256: str
    json_pointer: Literal['/daily']


class CandidateSeriesData(ResponseModel):
    dataset_id: str
    dataset_sha256: str
    version: str
    data_kind: str
    universe_size: int = Field(ge=3)


class CandidateSeriesEvaluation(ResponseModel):
    split: Literal['validation']
    start: str
    end: str
    min_assets: int = Field(ge=3)


class CandidateSeriesPoint(ResponseModel):
    signal_date: str
    entry_date: str | None
    exit_date: str | None
    status: Literal['evaluated', 'skipped', 'purged']
    reason: str | None
    gross_return: float | None
    cumulative_gross_return: float | None
    rank_ic: float | None = Field(ge=-1.000000000001, le=1.000000000001)
    rank_ic_state: Literal['defined', 'constant_forward_returns', 'not_evaluated']
    available_assets: int | None = Field(ge=0)
    coverage: float | None = Field(ge=0, le=1)


class CandidateSeriesSummary(ResponseModel):
    sum_gross_return: float | None
    mean_gross_return: float | None
    mean_rank_ic: float | None
    evaluated_days: int = Field(ge=0)
    rank_ic_days: int = Field(ge=0)
    skipped_days: int = Field(ge=0)
    purged_days: int = Field(ge=0)
    factor_coverage: float = Field(ge=0, le=1)


class CandidateSeriesResponse(ResponseModel):
    schema_version: Literal[1]
    run_id: str
    research_id: str
    revision_id: str
    revision_digest: str
    attempt_id: str
    candidate_id: str
    # Exactly get_run.review_targets[].result_digest: digest(candidate), not
    # digest(candidate.result), whose identity is separately source.result_sha256.
    result_digest: str
    integrity: Literal['verified']
    status: Literal['evaluated', 'not_evaluable']
    reason: str | None
    source: CandidateSeriesSource
    data: CandidateSeriesData
    evaluation: CandidateSeriesEvaluation
    summary: CandidateSeriesSummary
    points: list[CandidateSeriesPoint] = Field(max_length=MAX_POINTS)
    limitations: list[str]
