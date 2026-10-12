import { useState } from "react";
import type { AcceptedMaterialDraft, PublicMaterialDetail } from "../generated/api-contract";
import { selectMaterialHypotheses } from "../researchCreationFlow";
import { useI18n } from "../i18n";
import { describeError } from "../domain";
import { Field, JsonDetails } from "./ui";
import { useDraft } from "../drafts";
import { applyMaterialEditingDraft, initialMaterialEdit, isMaterialEditingDraft, materialEditingKey } from "../materialEditingDraft";

export default function MaterialAdoptionPanel({ workspaceId, material, busy, onAdopt }: { workspaceId: string; material: PublicMaterialDetail; busy: boolean; onAdopt: (draft: AcceptedMaterialDraft) => Promise<boolean> }) {
  const { t } = useI18n();
  const edit = useDraft(materialEditingKey(workspaceId, material.id), initialMaterialEdit(material), isMaterialEditingDraft);
  const value = applyMaterialEditingDraft(material, edit.value), selected = edit.value.selectedIds;
  const stale = !!edit.pending && (edit.pending.value.materialId !== material.id || edit.pending.value.baseDigest !== material.current_digest);
  const [confirmed, setConfirmed] = useState(false), [error, setError] = useState("");
  function change(index: number, patch: Partial<AcceptedMaterialDraft["hypotheses"][number]>) {
    const id = value.hypotheses[index].id;
    edit.update(old => ({ ...old, hypotheses: { ...old.hypotheses, [id]: { ...old.hypotheses[id], ...patch } } })); setConfirmed(false);
  }
  return <section aria-label={t("材料提案核对与采纳", "Review and adopt material")}>
    <h3>{t("核对依据与研究假说", "Check evidence and hypotheses")}</h3>
    <p>{t("字面引用已由工具校验；采纳保存一个材料版本，不代表语义已通过人工评定。未经解决的规则继续保留并阻断候选生成。", "Tools validated literal citations. Adoption saves a material version, not a human semantic approval. Unresolved rules remain visible and block candidate generation.")}</p>
    {material.draft.evidence.map(item => <article key={item.id}><strong>{item.id} · {t(`物理第 ${item.page} 页`, `Physical page ${item.page}`)}</strong><blockquote>{item.quote}</blockquote><p className="fine-print">{item.chunk_id} · {item.normalized_start}—{item.normalized_end}</p><a className="text-link" href={`/api/papers/${material.paper_id}/pdf#page=${item.page}`} target="_blank" rel="noreferrer">{t("核对 PDF 原页", "Check original PDF page")}</a></article>)}
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
  </section>;
}
