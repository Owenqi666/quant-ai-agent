import { useEffect, useState } from "react";
import type { AcceptedMaterialDraft, PublicMaterialDetail, PublicMaterialDiagnostic } from "../generated/api-contract";
import { api } from "../api";
import { selectMaterialHypotheses } from "../researchCreationFlow";
import { useI18n } from "../i18n";
import { describeError } from "../domain";
import { Field, JsonDetails } from "./ui";
import { useDraft } from "../drafts";
import { applyMaterialEditingDraft, initialMaterialEdit, isMaterialEditingDraft, materialEditingKey } from "../materialEditingDraft";
import { engineeringDispositionsComplete, initialEngineeringDispositions, isGroundedMaterial } from "../materialEngineering";
import type { EngineeringDisposition, SourceMaterial } from "../materialEngineering";

export default function MaterialAdoptionPanel({ workspaceId, material, datasetId, busy, onAdopt, onEngineering }: { workspaceId: string; material: PublicMaterialDetail; datasetId?: string; busy: boolean; onAdopt: (draft: AcceptedMaterialDraft) => Promise<boolean>; onEngineering?: (items: EngineeringDisposition[]) => Promise<boolean> }) {
  const { t } = useI18n();
  const edit = useDraft(materialEditingKey(workspaceId, material.id), initialMaterialEdit(material), isMaterialEditingDraft);
  const value = applyMaterialEditingDraft(material, edit.value), selected = edit.value.selectedIds;
  const stale = !!edit.pending && (edit.pending.value.materialId !== material.id || edit.pending.value.baseDigest !== material.current_digest);
  const [confirmed, setConfirmed] = useState(false), [error, setError] = useState("");
  const source: SourceMaterial = material.latest_version?.material ?? material.draft;
  const grounded = isGroundedMaterial(source) ? source : null;
  const [dispositions, setDispositions] = useState(() => initialEngineeringDispositions(source));
  const manualEdits = JSON.stringify(value) !== JSON.stringify(source) || selected.length !== source.hypotheses.length;
  const [diagnostic, setDiagnostic] = useState<PublicMaterialDiagnostic | null>(null), [diagnosticError, setDiagnosticError] = useState("");
  const query = datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : "";
  useEffect(() => {
    let active = true;
    setDiagnostic(null); setDiagnosticError("");
    void api<PublicMaterialDiagnostic>(`/research-v2/materials/${material.id}/diagnostics${query}`).then(result => {
      if (!active) return;
      if (result.material_digest !== material.current_digest) { setDiagnosticError("材料版本已变化，请刷新已有状态后核对诊断。"); return; }
      setDiagnostic(result);
    }).catch(cause => { if (active) setDiagnosticError(describeError(cause)); });
    return () => { active = false; };
  }, [material.id, material.current_digest, query]);
  function change(index: number, patch: Partial<AcceptedMaterialDraft["hypotheses"][number]>) {
    const id = value.hypotheses[index].id;
    edit.update(old => ({ ...old, hypotheses: { ...old.hypotheses, [id]: { ...old.hypotheses[id], ...patch } } })); setConfirmed(false);
  }
  const definitionRecord = material.latest_version?.definition_repair;
  const definitionsChanged = definitionRecord?.before_material_digest !== definitionRecord?.after_material_digest;
  return <section aria-label={t("材料提案核对与采纳", "Review and adopt material")}>
    <h3>{t("核对依据与研究假说", "Check evidence and hypotheses")}</h3>
    <p>{t("字面引用已由工具校验；采纳保存一个材料版本，不代表语义已通过人工评定。未经解决的规则继续保留并阻断候选生成。", "Tools validated literal citations. Adoption saves a material version, not a human semantic approval. Unresolved rules remain visible and block candidate generation.")}</p>
    <section aria-label={t("源材料自动诊断", "Automatic source-material diagnostics")}>
      <p>{t("原文未决状态与公式工程配置属于不同范围；工程配置不能把科学问题改为已解决。读取诊断和下载材料报告不调用模型。", "Source unknowns and formula engineering settings have different scopes; engineering settings cannot resolve scientific questions. Reading diagnostics or downloading the material report makes no model call.")}</p>
      {diagnostic ? <><p>{t("源材料状态", "Source-material status")}: {diagnostic.status} · {diagnostic.stop_reason || t("无阻断代码", "No stop code")} · pending_human</p><JsonDetails label={t("查看自动诊断与实际字段", "Inspect automatic diagnostics and actual fields")} value={diagnostic} /></> : <p role="status">{t("诊断尚未读取", "Diagnostics not loaded")}{diagnosticError ? `：${diagnosticError}` : ""}</p>}
      <a className="button small" href={`/api/research-v2/materials/${material.id}/report${query}`} download>{t("下载当前材料报告（无实验指标）", "Download current material report (no experiment metrics)")}</a>
    </section>
    {definitionRecord && material.latest_version && <section aria-label={definitionsChanged ? t("自动引用修复记录", "Automatic citation repair record") : t("自动引用核对记录", "Automatic citation check record")}>
      <p>{definitionsChanged ? t("已从本次所选原文补充定义引用并保存新版本。原模型输出保留，支持关系仍需人工审核。", "Definition citations were added from the selected source and saved as a new version. Original model output is retained; support still awaits human review.") : t("自动核对未发现算子定义引用缺口；本版本未修改材料。支持关系仍需人工审核。", "The automatic check found no operator-definition citation gap; this version leaves the material unchanged. Support still awaits human review.")} · actor=automation · human_approval=false · pending_human</p>
      <p className="fine-print">v{material.latest_version.version} · {material.latest_version.id} · {material.latest_version.digest}</p>
      <JsonDetails label={definitionsChanged ? t("查看修复来源与绑定变化", "Inspect repair source and binding changes") : t("查看核对来源与结果", "Inspect check source and result")} value={definitionRecord} />
    </section>}
    {source.evidence.map(item => <article key={item.id}><strong>{item.id} · {t(`物理第 ${item.page} 页`, `Physical page ${item.page}`)}</strong><blockquote>{item.quote}</blockquote><p className="fine-print">{item.chunk_id} · {item.normalized_start}—{item.normalized_end}</p><a className="text-link" href={`/api/papers/${material.paper_id}/pdf#page=${item.page}`} target="_blank" rel="noreferrer">{t("核对 PDF 原页", "Check original PDF page")}</a></article>)}
    {grounded && <section aria-label={t("模型支持判断与人工待审", "Model support judgments and pending human review")}>
      <h4>{t("模型声明的证据支持关系", "Model-declared evidence support")}</h4>
      <p>{t("引用字面定位由工具校验。原支持判断来自模型，引用关系可能包含单独记录的自动修复，尚未由本人审核；direct 或 inference 不代表论文主张已证明。人工质量状态：pending_human。", "Tools verify literal quote locations. Support judgments originated from the model; references may include separately recorded automated repair and await human review; direct or inference does not establish the paper claim. Human quality status: pending_human.")}</p>
      {grounded.support_bindings.map(binding => <article key={`${binding.hypothesis_id}:${binding.component}`}>
        <strong>{binding.hypothesis_id} · {binding.component}</strong><p>{binding.status} · {t("依据", "Evidence")}: {binding.evidence_ids.join(", ") || t("未提供", "Not provided")}</p><p>{binding.explanation}</p>
      </article>)}
      {!!grounded.paper_statistics.length && <details><summary>{t("原论文统计数字（非本次计算指标）", "Paper statistics (not current calculation metrics)")}</summary>
        {grounded.paper_statistics.map((statistic, index) => <p key={index}>{statistic.label}: {statistic.value} · {statistic.evidence_ids.join(", ")}</p>)}
      </details>}
    </section>}
    {edit.pending && <section className="info-note" aria-label={t("材料编辑草稿恢复", "Material edit draft recovery")}><p>{stale ? t("发现另一材料版本的编辑草稿，不能覆盖当前版本。可查看或丢弃原草稿。", "An edit draft belongs to a different material version and cannot overwrite this version. Inspect or discard it.") : t("发现未采纳的本机编辑，仅保存修改与选择；请明确恢复或丢弃。", "Unadopted local edits were found. Only changes and selections were saved; explicitly restore or discard them.")}</p><button type="button" className="button" disabled={busy || stale} onClick={() => { edit.restore(); setConfirmed(false); }}>{t("恢复材料编辑", "Restore material edits")}</button><button type="button" className="button" disabled={busy} onClick={() => { edit.discard(); setConfirmed(false); }}>{t("丢弃材料编辑", "Discard material edits")}</button><JsonDetails label={t("查看材料编辑草稿", "Inspect material edit draft")} value={edit.pending.value} /></section>}
    {edit.error && <p role="alert">{edit.error}</p>}
    <fieldset disabled={busy || !!edit.pending || !edit.ready}>
      {value.hypotheses.map((item, index) => <fieldset key={item.id}><legend>{item.id}</legend>
        <label><input type="checkbox" checked={selected.includes(item.id)} onChange={event => { edit.update(old => ({ ...old, selectedIds: event.target.checked ? [...old.selectedIds, item.id] : old.selectedIds.filter(id => id !== item.id) })); setConfirmed(false); }} />{t("采纳此假说", "Adopt this hypothesis")}</label>
        <Field label={t("研究主张", "Research claim")}><textarea value={item.claim} onChange={event => change(index, { claim: event.target.value, claim_attribution: "user_modification" })} /></Field>
        <p>{t("主张来源", "Claim attribution")}: {item.claim_attribution} · {t("引用", "Evidence")}: {item.evidence_ids.join(", ")}</p>
        <Field label={t("经济机制", "Economic mechanism")}><textarea value={item.economic_mechanism ?? ""} onChange={event => change(index, { economic_mechanism: event.target.value || null, mechanism_attribution: event.target.value ? "user_modification" : "unresolved" })} /></Field>
        <p>{t("机制来源", "Mechanism attribution")}: {item.mechanism_attribution}</p>
        <Field label={t("信号方向", "Signal direction")}><textarea value={item.signal_direction ?? ""} onChange={event => change(index, { signal_direction: event.target.value || null })} /></Field>
        <Field label={t("计算定义", "Calculation")}><textarea value={item.calculation ?? ""} onChange={event => change(index, { calculation: event.target.value || null })} /></Field>
        <p>{t("所需数据", "Required data")}: {item.required_data.map(field => `${field.name}: ${field.semantics}`).join("; ")}</p>
        <p>{t("适用条件", "Conditions")}: {item.conditions.join("; ") || t("未提供", "Not provided")}</p>
        <Field label={t("尚未解决的规则（每行一项）", "Unresolved rules (one per line)")}><textarea value={item.unresolved_rules.join("\n")} onChange={event => change(index, { unresolved_rules: event.target.value.split("\n").filter(line => line.trim()) })} /></Field>
      </fieldset>)}
      <Field label={t("材料级未决规则（每行一项）", "Material unresolved rules (one per line)")}><textarea value={value.unresolved_rules.join("\n")} onChange={event => { edit.update(old => ({ ...old, unresolvedRules: event.target.value.split("\n").filter(line => line.trim()) })); setConfirmed(false); }} /></Field>
      <p>{t("限制", "Limitations")}: {value.limitations.join("; ") || t("未提供", "Not provided")}</p>
      <label><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />{t("已核对选中的引用、归因及未决项，保存本次材料版本", "I checked selected citations, attribution and unresolved rules; save this material version")}</label>
      {error && <p role="alert">{error}</p>}
      <button className="button primary" type="button" disabled={!confirmed || !selected.length} onClick={() => { try { void onAdopt(selectMaterialHypotheses(value, selected)).then(saved => { if (saved) edit.clear(); }).catch(cause => setError(describeError(cause))); } catch (cause) { setError(describeError(cause)); } }}>{t("采纳选定材料", "Adopt selected material")}</button>
    </fieldset>
    {onEngineering && grounded && <section aria-label={t("独立公式工程配置", "Independent formula engineering settings")}>
      <h3>{t("保留未决问题，配置公式工程验证", "Preserve unknowns and configure formula verification")}</h3>
      <p>{t("此操作保存自动化工程配置，使用已保存材料的全部假说。原文未决项继续保留；不补写经济机制，不形成本人审核或研究有效性结论。", "This saves automated engineering settings for all hypotheses in the saved material. Source unknowns remain; it supplies no economic mechanism, human approval or research-validity conclusion.")}</p>
      <fieldset disabled={busy || !!edit.pending || !edit.ready || manualEdits}>
        {dispositions.map((item, index) => <fieldset key={item.path}><legend>{t(`未决项 ${index + 1}`, `Unknown ${index + 1}`)}</legend>
          <p>{item.original}</p>
          <Field label={t(`未决项 ${index + 1} 的处置`, `Disposition for unknown ${index + 1}`)}><select value={item.disposition} onChange={event => setDispositions(before => before.map((row, position) => position === index ? { ...row, disposition: event.target.value as EngineeringDisposition["disposition"] } : row))}>
            <option value="blocked">{t("保留阻断", "Keep blocked")}</option>
            {item.original.startsWith("[scientific]") && <option value="scientific_unresolved">{t("科学问题继续未决", "Science remains unresolved")}</option>}
            <option value="project_configuration">{t("采用项目执行配置", "Apply project execution settings")}</option>
          </select></Field>
          <Field label={t(`未决项 ${index + 1} 的处置理由`, `Rationale for unknown ${index + 1}`)}><textarea maxLength={2000} value={item.rationale} onChange={event => setDispositions(before => before.map((row, position) => position === index ? { ...row, rationale: event.target.value } : row))} /></Field>
        </fieldset>)}
        {!dispositions.length && <p>{t("没有显式未决规则；保存仍仅形成工程配置，人工质量待审。", "No explicit unknown rules were recorded. Saving still creates engineering settings only; human quality review remains pending.")}</p>}
        <button type="button" className="button" disabled={!engineeringDispositionsComplete(dispositions)} onClick={async () => { setError(""); try { await onEngineering?.(dispositions); } catch (cause) { setError(describeError(cause)); } }}>{t("保存公式工程配置（自动化）", "Save formula engineering settings (automation)")}</button>
      </fieldset>
      {manualEdits && <p role="status">{t("存在未采用的人工编辑或选择。先采用或丢弃这些编辑，再使用已保存材料配置工程验证。", "Unadopted edits or selections exist. Adopt or discard them before configuring engineering verification from saved material.")}</p>}
    </section>}
  </section>;
}
