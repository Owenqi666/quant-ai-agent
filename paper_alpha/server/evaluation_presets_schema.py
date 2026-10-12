"""Closed, read-only daily evaluation preset and validation contracts."""
from typing import Literal
from pydantic import Field
from .response_schemas import ResponseModel
from .material_context import MaterialMethodPolicy

class EvaluationBounds(ResponseModel):
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

class EvaluationSplits(ResponseModel):
    train: EvaluationBounds
    validation: EvaluationBounds
    test: EvaluationBounds

class EvaluationConfiguration(ResponseModel):
    splits: EvaluationSplits
    min_assets: int = Field(ge=3)

class EvaluationSplitPreview(EvaluationBounds):
    name: Literal["train", "validation", "test"]
    sessions: int = Field(ge=3)
    held_out: bool

class EvaluationPresetSource(ResponseModel):
    kind: Literal["project_development_example"]
    path: Literal["examples/alpha101/task.json"]
    sha256: str
    description_zh: str
    description_en: str

class EvaluationPreset(ResponseModel):
    id: Literal["alpha101-synthetic-development-v1"]
    title_zh: str
    title_en: str
    evaluation: EvaluationConfiguration
    configuration_digest: str
    source: EvaluationPresetSource
    compatible_dataset_sha256: str
    compatible: bool
    unavailable_reason: str | None
    splits: list[EvaluationSplitPreview]
    execution: dict[str, str | float]
    final_test_locked: Literal[True]
    method_policy: MaterialMethodPolicy
    warnings: list[str]

class EvaluationPresetList(ResponseModel):
    schema_version: Literal[1]
    dataset_id: str | None
    dataset_sha256: str | None
    items: list[EvaluationPreset]

class EvaluationPresetValidate(ResponseModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    evaluation: EvaluationConfiguration

class EvaluationPresetValidation(ResponseModel):
    schema_version: Literal[1]
    dataset_id: str
    dataset_sha256: str
    metadata_digest: str
    configuration_digest: str
    evaluation: EvaluationConfiguration
    splits: list[EvaluationSplitPreview]
    execution: dict[str, str | float]
    final_test_locked: Literal[True]
    method_policy: MaterialMethodPolicy
    warnings: list[str]
