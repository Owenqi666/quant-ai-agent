"""Explicit, result-bound research judgments; declared identity is not authentication."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DIMENSIONS = ('evidence_accuracy', 'hypothesis_fidelity', 'mechanism_attribution',
              'field_semantics', 'implementation_alignment')
MAX_ACTIVE_SECONDS = 24 * 60 * 60


class AssessmentModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class DimensionAssessment(AssessmentModel):
    outcome: Literal['passed', 'failed', 'not_assessed', 'not_applicable']
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('reason')
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError('Assessment reasons must not be blank')
        return value


class AssessmentDimensions(AssessmentModel):
    evidence_accuracy: DimensionAssessment
    hypothesis_fidelity: DimensionAssessment
    mechanism_attribution: DimensionAssessment
    field_semantics: DimensionAssessment
    implementation_alignment: DimensionAssessment


class ActiveInterval(AssessmentModel):
    # Strings keep the HTTP and persisted representation explicit, with no
    # Pydantic coercion from numeric timestamps or timezone-free datetimes.
    started_at: str = Field(min_length=20, max_length=40)
    ended_at: str = Field(min_length=20, max_length=40)

    @field_validator('started_at', 'ended_at')
    @classmethod
    def aware_timestamp(cls, value):
        parse_timestamp(value)
        return value


class ResearchAssessment(AssessmentModel):
    reviewer: str = Field(min_length=1, max_length=120)
    expected_attempt_id: str = Field(min_length=1, max_length=64)
    expected_result_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    dimensions: AssessmentDimensions
    active_intervals: list[ActiveInterval] = Field(max_length=100)

    @field_validator('reviewer', 'expected_attempt_id')
    @classmethod
    def nonblank_identity(cls, value):
        if not value.strip():
            raise ValueError('Assessment identities must not be blank')
        return value


def parse_timestamp(value):
    try:
        if 'T' not in value:
            raise ValueError('Use an ISO datetime with T and an explicit timezone')
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError('Active intervals require an explicit timezone')
        return timestamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Active intervals require valid ISO datetimes with an explicit timezone') from exc


def interval_seconds(intervals, *, current_time=None):
    ordered = sorted((parse_timestamp(item['started_at']), parse_timestamp(item['ended_at']))
                     for item in intervals)
    total, previous_end = 0.0, None
    for start, end in ordered:
        if end <= start:
            raise ValueError('Active intervals must have positive duration')
        if current_time is not None and end > current_time:
            raise ValueError('Active intervals cannot extend into the future')
        if previous_end is not None and start < previous_end:
            raise ValueError('Active intervals must not overlap')
        total += (end - start).total_seconds()
        if total > MAX_ACTIVE_SECONDS:
            raise ValueError('Declared active time cannot exceed 24 hours per assessment')
        previous_end = end
    return total


def validate_assessment(value, *, current_time=None):
    normalized = ResearchAssessment.model_validate(value).model_dump()
    interval_seconds(normalized['active_intervals'], current_time=current_time or datetime.now(timezone.utc))
    return normalized


def assessment_summary(assessment, source):
    if assessment is None:
        status = 'not_assessed'
    elif source != 'human':
        status = 'not_human'
    else:
        outcomes = [assessment['dimensions'][name]['outcome'] for name in DIMENSIONS]
        status = ('failed' if 'failed' in outcomes else
                  'passed' if all(outcome == 'passed' for outcome in outcomes) else 'incomplete')
    intervals = assessment['active_intervals'] if assessment else []
    return {'semantic_status': status, 'total_active_seconds': interval_seconds(intervals),
            'timing_recorded': bool(intervals), 'source_is_declared': True}
