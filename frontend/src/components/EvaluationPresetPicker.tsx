import { useEffect, useState } from "react";
import { api } from "../api";
import { describeError } from "../domain";
import type { JsonObject } from "../domain";
import type { EvaluationPresetList } from "../generated/api-contract";
import { useI18n } from "../i18n";
import { presetApplicable, presetResponseMatches, presetSelectionIsCurrent, selectEvaluationPreset } from "../evaluationPresets";
import type { PresetDataset, PresetSelection } from "../evaluationPresets";

export type EvaluationPresetPickerProps = {
  dataset?: PresetDataset; evaluation: JsonObject; sourceKey: string; disabled?: boolean; selection?: PresetSelection | null;
  onApply: (evaluation: JsonObject, selection: PresetSelection) => void;
};

/** Selecting only previews. An explicit click applies settings; no mutation API is called. */
export default function EvaluationPresetPicker({ dataset, evaluation, sourceKey, disabled = false, selection, onApply }: EvaluationPresetPickerProps) {
  const { locale, t } = useI18n();
  const [response, setResponse] = useState<EvaluationPresetList | null>(null);
  const [selected, setSelected] = useState("");
  const [applied, setApplied] = useState<PresetSelection | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setResponse(null); setSelected(""); setApplied(null); setError(""); setLoading(true);
    const query = dataset ? "?dataset_id=" + encodeURIComponent(dataset.id) : "";
    void api<EvaluationPresetList>("/evaluation-presets" + query, { signal: controller.signal })
      .then((value) => {
        if (!current || controller.signal.aborted) return;
        if (!presetResponseMatches(value, dataset)) throw new Error("The selected dataset changed. Reload its configuration options.");
        setResponse(value);
      }).catch((cause: unknown) => { if (current && !controller.signal.aborted) setError(describeError(cause)); })
      .finally(() => { if (current && !controller.signal.aborted) setLoading(false); });
    return () => { current = false; controller.abort(); };
  }, [dataset?.id, dataset?.sha256, sourceKey]);
  const item = response?.items.find((entry) => entry.id === selected);
  const remembered = selection === undefined ? applied : selection;
  const currentPreset = response?.items.find((entry) => entry.id === remembered?.presetId);
  const current = presetSelectionIsCurrent(remembered, dataset, evaluation, sourceKey, currentPreset) && (!response || !!currentPreset);
  return <section className="panel" aria-label={t("日度实验配置示例", "Daily evaluation settings")}>
    <label>{t("配置方式", "Configuration mode")} <select disabled={disabled || loading}
      value={selected} onChange={(event) => setSelected(event.target.value)}>
      <option value="">{t("手动配置", "Manual configuration")}</option>
      {response?.items.map((entry) => <option key={entry.id} value={entry.id}>{locale === "zh" ? entry.title_zh : entry.title_en}</option>)}
    </select></label>
    <p className="fine-print">{t("选择示例仅预览。点击应用后才替换时间配置，其他研究内容与预算保持原值。", "Selecting an example only previews it. Apply replaces evaluation settings while preserving the research content and budget.")}</p>
    {loading && <p role="status">{t("正在读取配置", "Loading settings")}</p>}
    {error && <p role="alert">{error}</p>}
    {remembered && <p role="status">{current
      ? t("示例配置已应用；请核对并确认本次实验的数据和时间设置。", "Example applied. Check and confirm this experiment's data and time settings.")
      : t("配置或来源已修改，需要重新确认。", "Settings or source changed; confirmation is required again.")}</p>}
    {item && <div>
      <p>{locale === "zh" ? item.source.description_zh : item.source.description_en}</p>
      <table><thead><tr><th>{t("区间", "Split")}</th><th>{t("开始", "Start")}</th><th>{t("结束", "End")}</th><th>{t("交易日", "Sessions")}</th></tr></thead>
        <tbody>{item.splits.map((split) => <tr key={split.name}><td>{split.name === "train" ? t("训练", "Train") : split.name === "validation" ? t("验证", "Validation") : t("最终测试（保留）", "Final test (held out)")}</td><td>{split.start}</td><td>{split.end}</td><td>{split.sessions}</td></tr>)}</tbody></table>
      <p>{t("最少有效资产数", "Minimum valid assets")}: {item.evaluation.min_assets}</p>
      <p>{t("仅计算验证区间；最终测试区间不被评估。", "Only the validation split is evaluated; final test remains held out.")}</p>
      {item.warnings.map((warning) => <p role="alert" key={warning}>{warning}</p>)}
      <details><summary>{t("执行规则与配置来源", "Execution rules and configuration source")}</summary>
        <dl>{Object.entries(item.execution).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{String(value)}</dd></div>)}</dl>
        <p>{item.source.path}</p><p>{t("配置摘要", "Configuration digest")}: <code>{item.configuration_digest}</code></p>
        <p>{t("适用数据指纹", "Compatible dataset fingerprint")}: <code>{item.compatible_dataset_sha256}</code></p>
      </details>
      {!presetApplicable(item, dataset) && <p role="alert">{t("此示例不适用于当前选择；可继续手动配置。", "This example is unavailable for the current selection; manual configuration remains available.")} {item.unavailable_reason}</p>}
      <button type="button" className="button" disabled={disabled || !presetApplicable(item, dataset)}
        onClick={() => {
          const next = selectEvaluationPreset(item, dataset, sourceKey);
          onApply(next.evaluation, next.selection); setApplied(next.selection);
        }}>{t("应用示例配置", "Apply example settings")}</button>
    </div>}
  </section>;
}
