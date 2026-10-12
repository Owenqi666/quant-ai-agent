import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import type { EvaluationPreset } from "./generated/api-contract";
import { applyEvaluationPreset, evaluationConfirmationKey, presetApplicable, presetResponseMatches, presetSelectionIsCurrent, selectEvaluationPreset } from "./evaluationPresets";

const example = JSON.parse(readFileSync(new URL("../../examples/alpha101/task.json", import.meta.url), "utf8"));
const dataset = { id: "dataset-one", sha256: "a".repeat(64) };
const preset: EvaluationPreset = {
  id: "alpha101-synthetic-development-v1", title_zh: "示例", title_en: "Example",
  evaluation: example.evaluation, configuration_digest: "b".repeat(64),
  source: { kind: "project_development_example", path: "examples/alpha101/task.json", sha256: "c".repeat(64), description_zh: "项目选择", description_en: "Project choice" },
  compatible_dataset_sha256: dataset.sha256, compatible: true, unavailable_reason: null,
  splits: [], execution: {}, final_test_locked: true, warnings: [],
  method_policy: { schema_version: 1, kind: "daily_synthetic_v1", operator_semantics_version: "fixture", allow_field_substitution: false, evaluation_period: "validation" },
};
describe("explicit daily preset application", () => {
  it("only replaces evaluation and never adds preset provenance to Task", () => {
    const task = { ...example, evaluation: { splits: {}, min_assets: 3 } };
    const before = structuredClone(task);
    const applied = applyEvaluationPreset(task, preset, dataset, "source-one");
    expect(applied.evaluation).toEqual(example.evaluation);
    for (const key of ["evidence", "hypotheses", "candidates", "budget"]) expect(applied[key]).toEqual(before[key]);
    expect(Object.keys(applied)).toEqual(Object.keys(task)); expect(task).toEqual(before);
    applied.evaluation.min_assets = 99;
    expect(preset.evaluation.min_assets).toBe(example.evaluation.min_assets);
  });
  it("refuses same-title or stale data with another fingerprint", () => {
    expect(presetApplicable(preset, undefined)).toBe(false);
    expect(() => selectEvaluationPreset(preset, { ...dataset, sha256: "d".repeat(64) }, "source")).toThrow();
    expect(() => selectEvaluationPreset({ ...preset, compatible: false }, dataset, "source")).toThrow();
  });
  it("date, data, source version and method configuration edits invalidate confirmation", () => {
    const selected = selectEvaluationPreset(preset, dataset, "paper:version-one");
    expect(presetSelectionIsCurrent(selected.selection, dataset, selected.evaluation, "paper:version-one")).toBe(true);
    expect(presetSelectionIsCurrent(selected.selection, dataset, { ...selected.evaluation, min_assets: 9 }, "paper:version-one")).toBe(false);
    expect(presetSelectionIsCurrent(selected.selection, { ...dataset, id: "another" }, selected.evaluation, "paper:version-one")).toBe(false);
    expect(presetSelectionIsCurrent(selected.selection, dataset, selected.evaluation, "paper:version-two")).toBe(false);
    const restored = JSON.parse(JSON.stringify(selected));
    expect(presetSelectionIsCurrent(restored.selection, dataset, restored.evaluation, "paper:version-one")).toBe(true);
  });
  it("compares normalized key order without inventing a server digest", () => {
    expect(evaluationConfirmationKey(dataset, { min_assets: 3, splits: {} }, "source"))
      .toEqual(evaluationConfirmationKey(dataset, { splits: {}, min_assets: 3 }, "source"));
  });
  it("rejects a late response for a different selected dataset", () => {
    const response = { schema_version: 1 as const, dataset_id: dataset.id, dataset_sha256: dataset.sha256, items: [preset] };
    expect(presetResponseMatches(response, dataset)).toBe(true);
    expect(presetResponseMatches(response, { ...dataset, id: "another" })).toBe(false);
    expect(presetResponseMatches(response, undefined)).toBe(false);
  });
});

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import EvaluationPresetPicker from "./components/EvaluationPresetPicker";
it("restores applied configuration status and marks changed source as needing confirmation", () => {
  const selected = selectEvaluationPreset(preset, dataset, "source-one");
  const props = { dataset, evaluation: selected.evaluation, sourceKey: "source-one", selection: selected.selection, onApply: () => { throw new Error("Rendering cannot apply"); } };
  expect(renderToStaticMarkup(createElement(EvaluationPresetPicker, props))).toContain("示例配置已应用");
  expect(renderToStaticMarkup(createElement(EvaluationPresetPicker, { ...props, sourceKey: "source-two" }))).toContain("需要重新确认");
});

it("invalidates a restored label when the server preset source version changes", () => {
  const selected = selectEvaluationPreset(preset, dataset, "source");
  expect(presetSelectionIsCurrent(selected.selection, dataset, selected.evaluation, "source", preset)).toBe(true);
  expect(presetSelectionIsCurrent(selected.selection, dataset, selected.evaluation, "source",
    { ...preset, source: { ...preset.source, sha256: "e".repeat(64) } })).toBe(false);
});
