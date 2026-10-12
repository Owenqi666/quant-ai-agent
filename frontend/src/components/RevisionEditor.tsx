import { useEffect, useState } from "react";
import { describeError, editableTask } from "../domain";
import type { Dataset, Research, Revision, Task } from "../domain";
import { draftKey, isResearchDraft, useDraft } from "../drafts";
import { formTask, taskFormIssues } from "../taskEditor";
import type { TaskEditorSection } from "../taskEditor";
import { taskDataIssues } from "../datasetImports";
import DraftNotice from "./DraftNotice";
import TaskEditor, { localizeTaskIssue, usePaper } from "./TaskEditor";
import { Field } from "./ui";

import { useI18n } from "../i18n";
import "./experimentFlow.css";

export default function RevisionEditor({ research, revision, dataset, workspaceId, busy, onSave, onCancel, onDirtyChange }: {
  research: Research; revision: Revision; dataset?: Dataset; workspaceId: string; busy: string;
  onSave: (task: Task, note: string, draftKey: string) => Promise<void>; onCancel: () => void; onDirtyChange?: (dirty: boolean) => void;
}) {
  const { t } = useI18n();
  const draft = useDraft(workspaceId ? draftKey(workspaceId, `revision:${research.id}:${revision.id}`) : null,
    { title: research.title, paperId: research.paper_id, datasetId: research.dataset_id, taskText: JSON.stringify(editableTask(revision.task), null, 2), note: "" }, isResearchDraft);
  useEffect(() => { onDirtyChange?.(draft.dirty || !!draft.pending); }, [draft.dirty, draft.pending, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  const { paper, error: paperError } = usePaper(research.paper_id);
  const [stage, setStage] = useState<Exclude<TaskEditorSection, "all">>("evidence");
  const errors: string[] = [];
  const warnings: string[] = [];
  let task: Task | null = null;
  try {
    task = formTask(draft.value.taskText);
    errors.push(...taskFormIssues(task, paper));
    if (dataset) { const checks = taskDataIssues(task, dataset); errors.push(...checks.errors); warnings.push(...checks.warnings); }
    else errors.push("固定数据版本尚未加载。");
  } catch (error) { errors.push(describeError(error)); }
  return <div className="revision-editor experiment-flow">
    <div className="info-note">{t(`从 v${revision.number} 创建新版本。请同步修改归属、依据与变更说明；历史实验保持原样。数据版本保持固定。`, `Create a new revision from v${revision.number}. Update attribution, evidence and change notes together. Historical experiments and the fixed dataset version stay unchanged.`)}</div>
    <DraftNotice pending={draft.pending} error={draft.error} enabled={!!workspaceId} onRestore={draft.restore} onDiscard={draft.discard} />
    <fieldset className="editor-container" disabled={!!draft.pending || !draft.ready || !!busy}>
      <nav className="button-row flow-editor-nav" aria-label={t("修订步骤", "Revision steps")}>
        {([ ["evidence", "证据与假说", "Evidence and hypotheses"], ["candidates", "候选", "Candidates"], ["experiment", "实验配置", "Experiment configuration"] ] as const).map(([id, label, english], index) =>
          <button key={id} type="button" className={`button small${stage === id ? " active" : ""}`} aria-current={stage === id ? "step" : undefined} onClick={() => setStage(id)}>{index + 1}. {t(label, english)}</button>)}
      </nav>
      {paperError && <p className="info-note warning" role="alert">{paperError}</p>}
      <TaskEditor value={draft.value.taskText} onChange={(taskText) => draft.update({ ...draft.value, taskText })} paper={paper} dataset={dataset} baseTask={editableTask(revision.task)} section={stage} showIssues={stage !== "experiment"} />
      {stage === "experiment" && !!errors.length && <div className="info-note warning" role="alert"><p>{t("保存前需要调整完整任务；返回对应步骤修改不会丢失草稿。", "Resolve the complete task before saving. Returning to an earlier step preserves your draft.")}</p><ul>{errors.map((message) => <li key={message}>{localizeTaskIssue(message, t)}</li>)}</ul></div>}
      {(stage === "candidates" || stage === "experiment") && !!warnings.length && <div className="info-note"><ul>{warnings.map((message) => <li key={message}>{localizeTaskIssue(message, t)}</li>)}</ul></div>}
      {stage === "candidates" && <p className="fine-print">{t("该修订的完整候选集合会进入实验；如需缩小范围，在保存前明确删除不参与的候选。", "The complete candidate set in this revision will be submitted. Delete unwanted candidates explicitly before saving.")}</p>}
      {stage === "experiment" && <>
      <Field label={t("修改说明", "Change note")}><input value={draft.value.note} onChange={(e) => draft.update({ ...draft.value, note: e.target.value })} placeholder={t("本次修改了什么，为什么修改？", "What changed, and why?")} /></Field>
      <p className="fine-print">{t("保存新版本不会运行实验。历史实验与已有审核继续绑定原版本，随后可明确提交此新版本。", "Saving a revision does not run an experiment. Historical experiments and reviews remain bound to their original revision. You can explicitly submit the new revision after saving.")}</p>
      </>}
      <div className="button-row flow-decision">
        {stage !== "evidence" && <button type="button" className="button" onClick={() => setStage(stage === "experiment" ? "candidates" : "evidence")}>{t("上一步", "Previous step")}</button>}
        {stage !== "experiment" ? <button type="button" className="button primary" disabled={!paper || !task || !!taskFormIssues(task, paper, stage).length} onClick={() => setStage(stage === "evidence" ? "candidates" : "experiment")}>{stage === "evidence" ? t("继续定义候选", "Continue to candidates") : t("继续配置实验", "Continue to configuration")}</button> : <button type="button" className="button primary" disabled={!!busy || !draft.value.note.trim() || errors.length > 0 || !paper} onClick={() => {
        if (task) void onSave(task, draft.value.note, draftKey(workspaceId, `revision:${research.id}:${revision.id}`));
      }}>{busy === "保存新版本" ? t("保存中…", "Saving…") : t("保存新版本", "Save new revision")}</button>}
        <button type="button" className="button" onClick={onCancel}>{t("取消编辑", "Cancel editing")}</button>
      </div>
    </fieldset>
  </div>;
}
