import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { describeError, shortId } from "../domain";
import { post } from "../api";
import type { JsonObject } from "../domain";
import type { EvaluationPresetValidation } from "../generated/api-contract";
import ResearchCreationAI from "./ResearchCreationAI";
import type { CreationExperimentTarget } from "./ResearchCreationAI";
import type { Dataset, Paper, Task, Research } from "../domain";
import { recoveryKey, removeDurably } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { creationDraftKey } from "../submissionRecovery";
import { taskDataIssues } from "../datasetImports";
import DatasetImportPanel from "./DatasetImportPanel";
import DailySnapshotImportPanel from "./DailySnapshotImportPanel";
import EvaluationPresetPicker from "./EvaluationPresetPicker";
import { Field, JsonDetails } from "./ui";
import TaskEditor, { localizeTaskIssue, usePaper } from "./TaskEditor";
import DraftNotice from "./DraftNotice";
import { draftKey, useDraft } from "../drafts";
import { creationStage, isCreationDraft } from "../creationDrafts";
import type { CreationDraft, CreationStage } from "../creationDrafts";
import { emptyTask, formTask, taskFormIssues } from "../taskEditor";

import { useI18n } from "../i18n";
import "./experimentFlow.css";

const creationStages: { id: CreationStage; label: string; english: string }[] = [
  { id: "materials", label: "材料输入", english: "Materials" }, { id: "evidence", label: "证据与假说", english: "Evidence and hypotheses" },
  { id: "candidates", label: "候选", english: "Candidates" }, { id: "experiment", label: "实验配置", english: "Experiment configuration" },
];

export default function CreateResearch({
  papers,
  datasets,
  busy,
  onCancel,
  onUpload,
  onCreated,
  onError,
  onRefreshDatasets,
  initialTask,
  initialPaperId,
  workspaceId,
  draftScope = "blank",
  onDirtyChange,
  resumeRequested = false,
  onOpenExperiment,
}: {
  papers: Paper[];
  datasets: Dataset[];
  busy: string;
  onCancel: () => void;
  onUpload: (form: FormData) => Promise<Paper>;
  onCreated: (research: Research) => Promise<void>;
  onError: (message: string) => void;
  onRefreshDatasets: () => Promise<void>;
  initialTask?: Task;
  initialPaperId?: string;
  workspaceId: string;
  draftScope?: string;
  onDirtyChange?: (dirty: boolean) => void;
  resumeRequested?: boolean;
  onOpenExperiment: (target: CreationExperimentTarget) => void;
}) {
  const { t } = useI18n();
  const originalDraftKey = draftKey(workspaceId, `create:${draftScope}`);
  const mutation = useRecoverableMutation<Research>(recoveryKey(workspaceId, "research-request"), "/researches");
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  const [checkingConfiguration, setCheckingConfiguration] = useState(false);
  const [checkedConfiguration, setCheckedConfiguration] = useState<{ key: string; result: EvaluationPresetValidation } | null>(null);
  const configurationRequest = useRef(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const draft = useDraft<CreationDraft>(workspaceId ? originalDraftKey : null, {
    title: "", paperId: initialPaperId || papers[0]?.id || "", datasetId: datasets[0]?.id || "",
    taskText: JSON.stringify(initialTask || emptyTask(), null, 2), note: "", creationStage: "materials", creationMode: "manual",
  }, isCreationDraft);
  useEffect(() => { onDirtyChange?.(draft.dirty || !!draft.pending); }, [draft.dirty, draft.pending, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  const { title, paperId, datasetId, taskText } = draft.value;
  const aiMode = draft.value.creationMode === "ai";
  const setTitle = (title: string) => draft.update((previous) => ({ ...previous, title }));
  const setPaperId = (paperId: string) => { draft.update((previous) => ({ ...previous, paperId })); setConfirmed(false); };
  const setDatasetId = (datasetId: string) => { draft.update((previous) => ({ ...previous, datasetId })); setConfirmed(false); };
  const setTaskText = (taskText: string) => { draft.update((previous) => ({ ...previous, taskText })); setConfirmed(false); };
  const { paper, error: paperError } = usePaper(paperId);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [paperTitle, setPaperTitle] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const stage = creationStage(draft.value);
  const setStage = (next: CreationStage) => draft.update(previous => ({ ...previous, creationStage: next }));
  const restoreIntent = useRef(resumeRequested);
  useEffect(() => {
    if (!restoreIntent.current || !draft.ready || !mutation.ready) return;
    restoreIntent.current = false;
    if (draft.pending && !mutation.pending && !mutation.blocked) {
      draft.restore();
      setConfirmed(false);
    }
    // The inbox explicitly selected this draft; the intent applies once to this mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft.ready, draft.pending, mutation.ready, mutation.pending, mutation.blocked]);
  const dataset = datasets.find((value) => value.id === datasetId);
  let editingTask: Task | null = null;
  try { editingTask = formTask(taskText); } catch { /* TaskEditor presents the malformed-input error. */ }
  const configurationSource = `${paperId}:${paper?.sha256 || ""}`;
  const configurationKey = JSON.stringify({ workspaceId, title: title.trim(), paperId, paperDigest: paper?.sha256,
    datasetId, datasetDigest: dataset?.sha256, evaluation: editingTask?.evaluation, materialVersion: draft.value.creationFlow?.materialVersionId });
  const liveConfiguration = useRef(configurationKey); liveConfiguration.current = configurationKey;
  const configurationConfirmed = checkedConfiguration?.key === configurationKey;
  async function confirmGenerationConfiguration() {
    if (!dataset || !editingTask || !title.trim()) return;
    const request = ++configurationRequest.current, key = configurationKey;
    setCheckingConfiguration(true); setCheckedConfiguration(null);
    try {
      const result = await post<EvaluationPresetValidation>("/evaluation-presets/validate", { dataset_id: dataset.id, evaluation: editingTask.evaluation });
      if (mounted.current && request === configurationRequest.current && key === liveConfiguration.current && result.dataset_sha256 === dataset.sha256)
        setCheckedConfiguration({ key, result });
    } catch (cause) { if (mounted.current && key === liveConfiguration.current) onError(describeError(cause)); }
    finally { if (mounted.current && request === configurationRequest.current) setCheckingConfiguration(false); }
  }
  const applyPreset = (evaluation: JsonObject, selection: NonNullable<CreationDraft["creationPreset"]>) => {
    draft.update(previous => ({ ...previous, creationPreset: selection,
      taskText: JSON.stringify({ ...formTask(previous.taskText), evaluation }, null, 2) }));
    setConfirmed(false); setCheckedConfiguration(null);
  };
  let stageIssues: string[] = [];
  if (stage === "evidence" || stage === "candidates") {
    try { stageIssues = taskFormIssues(formTask(taskText), paper, stage); }
    catch (error) { stageIssues = [describeError(error)]; }
  }
  let dataIssues = { errors: [] as string[], warnings: [] as string[] };
  if (dataset) {
    try {
      const task = formTask(taskText);
      dataIssues = taskDataIssues(task, dataset);
      dataIssues.errors.push(...taskFormIssues(task, paper));
    }
    catch (error) { dataIssues.errors = [describeError(error)]; }
  }
  async function upload(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    setUploading(true);
    try {
      const f = new FormData();
      f.append("file", file);
      f.append("title", paperTitle.trim() || file.name);
      const p = await onUpload(f);
      setPaperId(p.id);
      setFile(null);
    } catch (e) {
      onError(describeError(e));
    } finally {
      setUploading(false);
    }
  }
  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      if (!title.trim() || !paper || !confirmed || !dataset || dataIssues.errors.length) throw new Error(t("请检查研究标题、论文、所选数据版本与完整任务配置，并确认时间切分后保存研究。", "Check the research title, paper, dataset version and complete task, then confirm the time windows before saving."));
      await save({
        title: title.trim(),
        paper_id: paperId,
        dataset_id: datasetId,
        task: formTask(taskText),
      });
    } catch (e) {
      onError(describeError(e));
    }
  }
  async function save(body?: Record<string, unknown>) {
    const context = body ? { draft_key: originalDraftKey } : mutation.pending?.context;
    setSaving(true);
    try {
      // Resolve the original draft identity before any request can be sent.
      const cleanupKey = creationDraftKey(workspaceId, context);
      const created = await mutation.execute(body, context);
      try {
        mutation.acknowledge(() => removeDurably(localStorage, cleanupKey));
      } catch (e) {
        if (mounted.current) setSavedNotice(t("研究已创建；本机记录尚未清理，原请求已保留，请安全重试确认。", "Research was created. The local record could not be cleared; safely retry the retained original request to confirm.") + describeError(e));
        return;
      }
      if (mounted.current) {
        if (cleanupKey === originalDraftKey) draft.clear();
        setSavedNotice(t("研究已创建。", "Research created."));
        onDirtyChange?.(false);
        try { await onCreated(created); }
        catch (e) { if (mounted.current) setSavedNotice(t("研究已创建，列表刷新暂未成功。", "Research was created, but refreshing the list failed.") + describeError(e)); }
      }
    } catch (e) { if (mounted.current) onError(describeError(e)); }
    finally { if (mounted.current) setSaving(false); }
  }
  return (
    <section className="panel create-panel experiment-flow">
      <div className="panel-heading">
        <div>
          <h2>{t("从论文创建研究", "Create research from a paper")}</h2>
        </div>
        <button className="button small" onClick={onCancel}>{t("返回工作台", "Back to workbench")}</button>
      </div>
      {savedNotice && <p role="status">{savedNotice}</p>}
      {mutation.error && <p role="alert">{mutation.error}</p>}
      {mutation.pending && <section aria-label={t("研究创建恢复", "Research creation recovery")} className="info-note">
        <p>{mutation.pending.state === "rejected" ? t("创建请求已明确拒绝，可以解除后修改。", "The creation request was explicitly rejected. Release it before editing.") : t("研究创建结果待确认。安全重试原请求可以确认是否已创建，不会生成第二项研究。", "The creation outcome is uncertain. Safely retry the original request to confirm it without creating a second research item.")}</p>
        <JsonDetails value={mutation.pending.body} label={t("查看冻结创建请求", "View frozen creation request")} />
        {mutation.pending.state === "rejected" ? <button className="button" disabled={!!busy || saving} onClick={mutation.dismissRejected}>{t("解除被拒绝的创建请求", "Release rejected creation request")}</button> :
          <button className="button" disabled={!!busy || saving} onClick={() => void save()}>{t("安全重试研究创建", "Safely retry research creation")}</button>}
      </section>}
      <DraftNotice pending={draft.pending} error={draft.error} enabled={!!workspaceId} onRestore={() => { draft.restore(); setConfirmed(false); }} onDiscard={() => { draft.discard(); setConfirmed(false); }} />
      <fieldset disabled={!!draft.pending || !draft.ready || !!busy || saving || !!mutation.pending || !mutation.ready || mutation.blocked} className="editor-container">
      <p className="info-note">{aiMode ? t("先核对并采纳论文材料，再确认数据与配置、生成候选。只有明确点击“保存此 AI 研究并提交实验”才会创建研究和提交运行。", "Review and adopt source material, confirm settings, then generate a candidate. Only the explicit Save this AI study and submit the experiment action creates the study and submits a run.") : t("研究定义仍是草稿。分步整理材料与候选，完成实验配置后统一保存；保存不会自动运行实验。", "The research definition is a draft. Organize the source and candidates, then save the complete configuration. Saving does not run an experiment.")}</p>
      <Field label={t("研究方式", "Research mode")}><select disabled={aiBusy || checkingConfiguration} value={aiMode ? "ai" : "manual"} onChange={event => {
        draft.update(previous => ({ ...previous, creationMode: event.target.value as "manual" | "ai" }));
        setConfirmed(false); setCheckedConfiguration(null);
      }}><option value="manual">{t("手动整理", "Manual research")}</option><option value="ai">{t("AI 辅助整理", "AI-assisted research")}</option></select></Field>
      <nav className="button-row flow-editor-nav" aria-label={t("新研究步骤", "New research steps")}>
        {creationStages.map((item, index) => <button key={item.id} type="button" className={`button small${stage === item.id ? " active" : ""}`} aria-current={stage === item.id ? "step" : undefined} disabled={aiBusy || checkingConfiguration} onClick={() => setStage(item.id)}>{index + 1}. {t(item.label, item.english)}</button>)}
      </nav>
      {stage === "materials" && <fieldset disabled={aiBusy || checkingConfiguration}><section aria-label={t("新研究材料输入", "New research materials")}>
        <h3>{t("选择论文与数据", "Choose a paper and dataset")}</h3>
        <Field label={t("研究标题", "Research title")}><input required value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("例如：价格与成交量关系研究", "For example: price and volume relationships")} /></Field>
        <Field label={t("研究论文", "Research paper")}><select required value={paperId} onChange={(e) => setPaperId(e.target.value)}>
          <option value="">{t("选择已上传论文", "Choose an uploaded paper")}</option>{papers.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
        </select></Field>
        {paperId && <a className="text-link" href={`/api/papers/${paperId}/pdf`} target="_blank" rel="noreferrer">{t("查看所选论文 ↗", "View selected paper \u2197")}</a>}
        {paper && <p className="fine-print">{t(`论文已登记，已提取 ${paper.pages?.length ?? 0} 页；研究定义尚未保存。`, `Paper registered with ${paper.pages?.length ?? 0} extracted pages; the research definition has not been saved.`)}</p>}
        <details>
          <summary>{t("上传新的论文 PDF", "Upload a new paper PDF")}</summary>
          <form className="upload-zone" onSubmit={upload}>
            <p>{t("PDF 文件 · 保留原文与页码", "PDF file \u00b7 preserves source text and page numbers")}</p>
            <Field label={t("论文文件", "Paper file")}><input type="file" accept="application/pdf,.pdf" onChange={(e) => setFile(e.target.files?.[0] || null)} /></Field>
            <Field label={t("论文标题", "Paper title")}><input value={paperTitle} onChange={(e) => setPaperTitle(e.target.value)} placeholder={t("可选，默认使用文件名", "Optional; defaults to the filename")} /></Field>
            <button className="button" disabled={!file || uploading || !!busy} type="submit">{uploading ? t("上传与提取中…", "Uploading and extracting…") : t("上传论文", "Upload paper")}</button>
          </form>
        </details>
        <Field label={t("数据版本", "Dataset version")}><select required value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
          <option value="">{t("选择数据集", "Choose a dataset")}</option>{datasets.map((d) => <option key={d.id} value={d.id}>{d.version} · {d.data_kind === "synthetic" ? t("合成数据", "Synthetic data") : d.data_kind} · {shortId(d.sha256)}</option>)}
        </select></Field>
        {dataset && <div className="info-note"><p>{dataset.title} · {dataset.version} · {t("摘要", "Digest")} {shortId(dataset.sha256)}</p>
          <p>{t("可用字段：", "Available fields: ")}{dataset.fields.join(", ")}{t("。保存后研究固定绑定这一数据版本，换数据需创建新研究。", ". Saving binds the research to this dataset version. Changing data requires new research.")}</p>
          <JsonDetails value={{ fields: dataset.fields, metadata: dataset.metadata, sha256: dataset.sha256 }} label={t("查看所选数据的日历、字段与完整元信息", "View dataset calendar, fields and complete metadata")} />
        </div>}
        {dataset?.data_kind === "daily_development_snapshot" && <div className="info-note">
          <p>{t("此快照只验证开发区间，最终值仍未知。研究使用登记时冻结的时间配置。", "Only development data was validated; final values remain unknown. Research uses the time settings frozen at registration.")}</p>
          <button type="button" className="button" disabled={!editingTask || !dataset.metadata.declared_evaluation} onClick={() => {
            if (editingTask && dataset.metadata.declared_evaluation && typeof dataset.metadata.declared_evaluation === "object") {
              setTaskText(JSON.stringify({ ...editingTask, evaluation: dataset.metadata.declared_evaluation }, null, 2));
            }
          }}>{t("应用登记的时间配置", "Apply registered time settings")}</button>
        </div>}
        <details><summary>{t("上传、验证并登记新的合成数据版本", "Upload, validate and register a new synthetic dataset version")}</summary><DatasetImportPanel onRegistered={async (id) => { await onRefreshDatasets(); setDatasetId(id); }} /></details>
        <details><summary>{t("登记日频开发快照", "Register a daily development snapshot")}</summary><DailySnapshotImportPanel workspaceId={workspaceId} onRegistered={async id => { await onRefreshDatasets(); setDatasetId(id); }} /></details>
        {paperError && <p className="info-note warning" role="alert">{paperError}</p>}
        {(!title.trim() || !paper || !dataset) && <p className="fine-print">{t("填写研究标题，并选择已加载的论文与已登记数据，才能继续。", "Enter a research title and choose a loaded paper and registered dataset to continue.")}</p>}
        <button type="button" className="button primary" disabled={!title.trim() || !paper || !dataset || uploading} onClick={() => setStage("evidence")}>{t("整理证据与假说", "Organize evidence and hypotheses")}</button>
      </section></fieldset>}
      {!aiMode && (stage === "evidence" || stage === "candidates") && <section aria-label={stage === "evidence" ? t("新研究证据与假说", "New research evidence and hypotheses") : t("新研究候选", "New research candidates")}>
        <p className="fine-print">{stage === "evidence" ? t("手动整理原文依据和解释。字面引用匹配不等于语义审核通过，模型接口无需启用。", "Organize the source evidence and interpretation manually. Literal matches do not establish semantic fidelity. No model connection is needed.") : t("下列候选都属于待保存任务。运行会提交该版本的完整候选集合；需要缩小范围时请明确删除不参与的候选。", "All candidates below belong to this draft. An experiment submits the revision’s complete candidate set; delete candidates explicitly before saving to reduce its scope.")}</p>
        {paperError && <p className="info-note warning" role="alert">{paperError}</p>}
        <TaskEditor value={taskText} onChange={setTaskText} paper={paper} dataset={dataset} section={stage} />
        {stage === "candidates" && !!dataIssues.warnings.length && <div className="info-note warning"><ul>{dataIssues.warnings.map((message) => <li key={message}>{localizeTaskIssue(message, t)}</li>)}</ul><p>{t("可以保留受阻候选作为失败记录；系统不会替换缺失字段。", "Blocked candidates can be retained as failure records; the system will not substitute missing fields.")}</p></div>}
        <div className="button-row"><button type="button" className="button" onClick={() => setStage(stage === "evidence" ? "materials" : "evidence")}>{t("上一步", "Previous step")}</button>
          <button type="button" className="button primary" disabled={!!stageIssues.length || !paper} onClick={() => setStage(stage === "evidence" ? "candidates" : "experiment")}>{stage === "evidence" ? t("继续定义候选", "Continue to candidates") : t("继续配置实验", "Continue to configuration")}</button>
        </div>
      </section>}
      {!aiMode && stage === "experiment" && <form onSubmit={create} aria-label={t("新研究实验配置", "New research experiment configuration")}>
        <h3>{t("确认完整研究，再进入实验", "Confirm the complete research definition")}</h3>
        <p>{title || t("尚未填写研究标题", "No research title")} · {paper?.title || t("尚未选择论文", "No paper selected")} · {dataset?.version || t("尚未选择数据", "No dataset selected")}</p>
        <p className="fine-print">{t("当前只保存完整研究定义。保存成功后进入实验阶段，由你明确提交该版本；原创建请求结果未知时会保留并安全恢复。", "This saves the complete research definition. You can then explicitly submit the saved revision as an experiment. An uncertain creation request is retained for safe recovery.")}</p>
        {editingTask && dataset?.data_kind !== "daily_development_snapshot" && <EvaluationPresetPicker dataset={dataset} evaluation={editingTask.evaluation} sourceKey={configurationSource} selection={draft.value.creationPreset}
          onApply={applyPreset} />}
        <TaskEditor value={taskText} onChange={setTaskText} paper={paper} dataset={dataset} section="experiment" showIssues={false} />
        {!!dataIssues.errors.length && <div className="info-note warning" role="alert"><strong>{t("保存前需要调整完整任务", "Resolve the complete task before saving")}</strong><ul>{dataIssues.errors.map((message) => <li key={message}>{localizeTaskIssue(message, t)}</li>)}</ul><p>{t("可通过上方步骤返回对应内容；草稿会保留。", "Return to the relevant step above; your draft is retained.")}</p></div>}
        {!!dataIssues.warnings.length && <div className="info-note"><ul>{dataIssues.warnings.map((message) => <li key={message}>{localizeTaskIssue(message, t)}</li>)}</ul><p>{t("负例仍可保留为研究任务；具体阻断记录由后端预检及实验生成。", "Negative cases can remain in the task. The server preflight and experiment produce the actual blocking records.")}</p></div>}
        {(!title.trim() || !paper || !dataset) && <p className="info-note warning">{t("材料未完整，请返回材料输入核对研究标题、论文与数据版本。", "Materials are incomplete. Return to the materials step to check the title, paper and dataset.")}</p>}
        <Field label={t("已确认所选数据版本及 train / validation / test 时间切分", "I confirm the selected dataset version and train / validation / test windows")}><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /></Field>
        <div className="button-row flow-decision"><button type="button" className="button" onClick={() => setStage("candidates")}>{t("上一步", "Previous step")}</button>
          <button className="button primary" disabled={!!busy || uploading || !paper || !dataset || !title.trim() || !confirmed || !!dataIssues.errors.length} type="submit">{saving ? t("保存中…", "Saving…") : t("保存研究并进入实验", "Save research and continue to experiment")}</button>
        </div>
      </form>}
      {aiMode && (stage === "candidates" || stage === "experiment") && <fieldset disabled={aiBusy || checkingConfiguration} aria-label={t("AI 生成配置", "AI generation configuration")}>
        <h3>{t("生成所需数据与时间设置", "Data and time settings for generation")}</h3>
        <p>{t("先确认配置，再生成候选。生成后修改这些配置会使原候选失效；最终测试保持保留。", "Confirm settings before generating. Later changes invalidate the previous candidate source; final test remains held out.")}</p>
        {editingTask && <EvaluationPresetPicker dataset={dataset} evaluation={editingTask.evaluation} sourceKey={configurationSource} selection={draft.value.creationPreset} onApply={applyPreset} />}
        <TaskEditor value={taskText} onChange={setTaskText} paper={paper} dataset={dataset} section="experiment" showIssues={false} configurationOnly />
        <button className="button" type="button" disabled={!dataset || !editingTask || !title.trim()} onClick={() => void confirmGenerationConfiguration()}>{checkingConfiguration ? t("检查配置中…", "Checking settings…") : t("核对并确认生成配置", "Check and confirm generation settings")}</button>
        {configurationConfirmed && <p role="status">{t("已确认此数据版本与时间设置；尚未调用模型。", "Dataset and time settings confirmed; no model call made.")}</p>}
        {configurationConfirmed && checkedConfiguration?.result.warnings.map(warning => <p role="status" key={warning}>{warning}</p>)}
      </fieldset>}
      {aiMode && draft.ready && !draft.pending && <ResearchCreationAI workspaceId={workspaceId} draftKey={originalDraftKey}
        paper={paper} dataset={dataset} title={title} evaluation={editingTask?.evaluation || {}} stage={stage}
        configurationConfirmed={configurationConfirmed} methodPolicy={checkedConfiguration?.result.method_policy || {}}
        value={draft.value.creationFlow} onChange={creationFlow => draft.update(previous => ({ ...previous, creationFlow }))}
        onStageChange={setStage} onBusyChange={setAiBusy} onOpenExperiment={onOpenExperiment}
        onApplyAuthorizedConfiguration={policy => {
          if (typeof policy.dataset_id !== "string" || typeof policy.title !== "string" || !policy.evaluation || typeof policy.evaluation !== "object") { onError(t("授权配置不完整，未更改草稿。", "Authorized settings are incomplete; the draft was not changed.")); return; }
          draft.update(previous => ({ ...previous, title: policy.title as string, datasetId: policy.dataset_id as string,
            taskText: JSON.stringify({ ...formTask(previous.taskText), evaluation: policy.evaluation }, null, 2) }));
          setConfirmed(false); setCheckedConfiguration(null);
        }} />}
      </fieldset>
    </section>
  );
}
