"""Read-only daily example settings; no candidate, job or engine is created."""
from copy import deepcopy
from pathlib import Path
import json
import hashlib

from ..evidence import sha256
from ..evaluation import EXECUTION, validate_config
from ..storage import digest, read_json
from .evaluation_presets_schema import (EvaluationConfiguration, EvaluationPresetList,
                                       EvaluationPresetValidation)
from .material_context import _dataset_source, default_material_method_policy
from .service import ServiceError

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "alpha101"
PRESET_ID = "alpha101-synthetic-development-v1"


def split_preview(evaluation, calendar):
    return [{"name": name, **bounds,
             "sessions": calendar.index(bounds["end"]) - calendar.index(bounds["start"]) + 1,
             "held_out": name == "test"}
            for name, bounds in evaluation["splits"].items()]


class EvaluationPresets:
    def __init__(self, store):
        self.store = store

    def validate(self, dataset_id, evaluation):
        """Validate actual dataset and dates before candidates exist."""
        if hasattr(evaluation, "model_dump"):
            evaluation = evaluation.model_dump()
        try:
            value = EvaluationConfiguration.model_validate(evaluation).model_dump()
            source = _dataset_source(self.store, dataset_id, value)
            row = self.store._fetch("datasets", dataset_id)
            metadata = json.loads(row["metadata"])
            if digest(metadata) != source["metadata_digest"]:
                raise ServiceError("Dataset changed during configuration validation", 409)
            value = validate_config(source["evaluation"], metadata["calendar_dates"])
            result = EvaluationPresetValidation.model_validate({
                "schema_version": 1, "dataset_id": dataset_id,
                "dataset_sha256": source["dataset_sha256"], "metadata_digest": source["metadata_digest"],
                "configuration_digest": digest(value), "evaluation": value,
                "splits": split_preview(value, metadata["calendar_dates"]),
                "execution": deepcopy(EXECUTION), "final_test_locked": True,
                "method_policy": default_material_method_policy(),
                "warnings": ([f"min_assets exceeds the registered universe of {len(metadata['universe'])} assets; validation cross sections will be insufficient."]
                             if value["min_assets"] > len(metadata["universe"]) else [])}).model_dump()
            if _dataset_source(self.store, dataset_id, value) != source:
                raise ServiceError("Dataset changed during configuration validation", 409)
            return result
        except ServiceError:
            raise
        except (ValueError, KeyError, OSError, TypeError) as exc:
            raise ServiceError("Evaluation configuration is invalid: " + self.store._safe_error(exc), 422) from exc

    def list(self, dataset_id=None):
        # The dates have one source. Neither frontend constants nor another
        # evaluation.json copy define this preset.
        task_path = EXAMPLE / "task.json"
        metadata = read_json(EXAMPLE / "metadata.json")
        expected_data = sha256(EXAMPLE / "market.csv")
        if metadata.get("market_sha256") != expected_data or metadata.get("data_kind") != "synthetic":
            raise ServiceError("Bundled example dataset integrity failed", 409)
        expected_dataset = digest({"data": expected_data, "metadata": metadata})
        task_bytes = task_path.read_bytes()
        evaluation = validate_config(json.loads(task_bytes)["evaluation"], metadata["calendar_dates"])
        actual_sha, compatible = None, False
        reason = "Choose a dataset to check this example configuration."
        if dataset_id is not None:
            row = self.store._fetch("datasets", dataset_id)
            actual_sha = row["sha256"]
            if actual_sha != expected_dataset:
                reason = "This preset requires the exact registered Alpha101 synthetic dataset fingerprint; use manual configuration for other data."
            else:
                try:
                    validated = self.validate(dataset_id, evaluation)
                    compatible = validated["dataset_sha256"] == expected_dataset
                    reason = None if compatible else "Dataset changed while reading the preset."
                except ServiceError as exc:
                    reason = self.store._safe_error(exc)
        item = {"id": PRESET_ID, "title_zh": "Alpha101 合成数据开发示例",
            "title_en": "Alpha101 synthetic development example", "evaluation": evaluation,
            "configuration_digest": digest(evaluation),
            "source": {"kind": "project_development_example", "path": "examples/alpha101/task.json",
                "sha256": hashlib.sha256(task_bytes).hexdigest(),
                "description_zh": "项目开发示例配置；这些日期不是 Alpha101 论文规定的窗口。",
                "description_en": "Project development settings; these dates are not windows prescribed by the Alpha101 paper."},
            "compatible_dataset_sha256": expected_dataset, "compatible": compatible,
            "unavailable_reason": reason, "splits": split_preview(evaluation, metadata["calendar_dates"]),
            "execution": deepcopy(EXECUTION), "final_test_locked": True,
            "method_policy": default_material_method_policy(), "warnings": []}
        return EvaluationPresetList.model_validate({"schema_version": 1, "dataset_id": dataset_id,
            "dataset_sha256": actual_sha, "items": [item]}).model_dump()
