import { describeError, editableTask } from "../domain";
import type { Dataset, Research, Revision, Task } from "../domain";
import { draftKey, isResearchDraft, useDraft } from "../drafts";
import { formTask, taskFormIssues } from "../taskEditor";
import { taskDataIssues } from "../datasetImports";
import DraftNotice from "./DraftNotice";
import TaskEditor, { usePaper } from "./TaskEditor";
import { Field } from "./ui";

export default function RevisionEditor({ research, revision, dataset, workspaceId, busy, onSave, onCancel }: {
  research: Research; revision: Revision; dataset?: Dataset; workspaceId: string; busy: string;
  onSave: (task: Task, note: string, draftKey: string) => Promise<void>; onCancel: () => void;
}) {
  const draft = useDraft(workspaceId ? draftKey(workspaceId, `revision:${research.id}:${revision.id}`) : null,
    { title: research.title, paperId: research.paper_id, datasetId: research.dataset_id, taskText: JSON.stringify(editableTask(revision.task), null, 2), note: "" }, isResearchDraft);
  const { paper, error: paperError } = usePaper(research.paper_id);
  const errors: string[] = [];
  const warnings: string[] = [];
  let task: Task | null = null;
  try {
    task = formTask(draft.value.taskText);
    errors.push(...taskFormIssues(task, paper));
    if (dataset) { const checks = taskDataIssues(task, dataset); errors.push(...checks.errors); warnings.push(...checks.warnings); }
    else errors.push("固定数据版本尚未加载。");
  } catch (error) { errors.push(describeError(error)); }
  return <div className="revision-editor">
    <div className="info-note">从 v{revision.number} 创建新版本。请同步修改归属、依据与变更说明；历史实验保持原样。数据版本保持固定。</div>
    <DraftNotice pending={draft.pending} error={draft.error} enabled={!!workspaceId} onRestore={draft.restore} onDiscard={draft.discard} />
    <fieldset className="editor-container" disabled={!!draft.pending || !draft.ready || !!busy}>
      {paperError && <p className="info-note warning" role="alert">{paperError}</p>}
      <TaskEditor value={draft.value.taskText} onChange={(taskText) => draft.update({ ...draft.value, taskText })} paper={paper} dataset={dataset} baseTask={editableTask(revision.task)} />
      {!!errors.length && <div className="info-note warning"><ul>{errors.map((message) => <li key={message}>{message}</li>)}</ul></div>}
      {!!warnings.length && <div className="info-note"><ul>{warnings.map((message) => <li key={message}>{message}</li>)}</ul></div>}
      <Field label="修改说明"><input value={draft.value.note} onChange={(e) => draft.update({ ...draft.value, note: e.target.value })} placeholder="本次修改了什么，为什么修改？" /></Field>
      <div className="button-row"><button type="button" className="button primary" disabled={!!busy || !draft.value.note.trim() || errors.length > 0 || !paper} onClick={() => {
        if (task) void onSave(task, draft.value.note, draftKey(workspaceId, `revision:${research.id}:${revision.id}`));
      }}>{busy === "保存新版本" ? "保存中…" : "保存新版本"}</button><button type="button" className="button" onClick={onCancel}>取消编辑</button></div>
    </fieldset>
  </div>;
}
