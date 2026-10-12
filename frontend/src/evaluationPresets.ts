import type { Dataset, JsonObject } from "./domain";
import type { EvaluationPreset, EvaluationPresetList } from "./generated/api-contract";

export type PresetDataset = Pick<Dataset, "id" | "sha256">;
export type PresetSelection = {
  presetId: string; configurationDigest: string; sourceDigest: string;
  datasetId: string; datasetSha256: string; confirmationKey: string;
};

/** Local comparison identity, not a server authorization or a cryptographic digest. */
function stable(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "undefined";
  if (Array.isArray(value)) return "[" + value.map(stable).join(",") + "]";
  return "{" + Object.entries(value as JsonObject).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => JSON.stringify(key) + ":" + stable(item)).join(",") + "}";
}
export function evaluationConfirmationKey(dataset: PresetDataset | undefined, evaluation: JsonObject, sourceKey: string): string {
  return stable({ dataset: dataset ? { id: dataset.id, sha256: dataset.sha256 } : null, evaluation, sourceKey });
}
export function presetResponseMatches(response: EvaluationPresetList, dataset: PresetDataset | undefined): boolean {
  return response.dataset_id === (dataset?.id ?? null) && response.dataset_sha256 === (dataset?.sha256 ?? null);
}
export function presetApplicable(preset: EvaluationPreset, dataset: PresetDataset | undefined): boolean {
  return !!dataset && preset.compatible && preset.unavailable_reason === null &&
    preset.compatible_dataset_sha256 === dataset.sha256 && preset.final_test_locked === true;
}
export function selectEvaluationPreset(preset: EvaluationPreset, dataset: PresetDataset | undefined, sourceKey: string) {
  if (!dataset || !presetApplicable(preset, dataset)) throw new Error("This preset is not compatible with the selected dataset.");
  const evaluation: JsonObject = structuredClone(preset.evaluation);
  const selection: PresetSelection = {
    presetId: preset.id, configurationDigest: preset.configuration_digest, sourceDigest: preset.source.sha256,
    datasetId: dataset.id, datasetSha256: dataset.sha256,
    confirmationKey: evaluationConfirmationKey(dataset, evaluation, sourceKey),
  };
  return { evaluation, selection };
}
/** Only evaluation is replaced. Evidence, hypotheses, candidates and budget retain their values. */
export function applyEvaluationPreset<T extends { evaluation: JsonObject }>(task: T, preset: EvaluationPreset, dataset: PresetDataset | undefined, sourceKey: string): T {
  return { ...task, evaluation: selectEvaluationPreset(preset, dataset, sourceKey).evaluation };
}
export function presetSelectionIsCurrent(selection: PresetSelection | null, dataset: PresetDataset | undefined, evaluation: JsonObject, sourceKey: string, currentPreset?: EvaluationPreset): boolean {
  return !!selection && selection.confirmationKey === evaluationConfirmationKey(dataset, evaluation, sourceKey) &&
    (!currentPreset || (selection.presetId === currentPreset.id && selection.sourceDigest === currentPreset.source.sha256 &&
      selection.configurationDigest === currentPreset.configuration_digest));
}
