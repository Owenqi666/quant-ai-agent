import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { describeError, shortId } from "../domain";
import type { Dataset, Paper, Task, Research } from "../domain";
import { recoveryKey, removeDurably } from "../reviewRecovery";
import { useRecoverableMutation } from "../useRecoverableMutation";
import { creationDraftKey } from "../submissionRecovery";
import { taskDataIssues } from "../datasetImports";
import DatasetImportPanel from "./DatasetImportPanel";
import { Field, JsonDetails } from "./ui";
import TaskEditor, { usePaper } from "./TaskEditor";
import DraftNotice from "./DraftNotice";
import { draftKey, isResearchDraft, useDraft } from "../drafts";
import { emptyTask, formTask, taskFormIssues } from "../taskEditor";

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
}) {
  const originalDraftKey = draftKey(workspaceId, `create:${draftScope}`);
  const mutation = useRecoverableMutation<Research>(recoveryKey(workspaceId, "research-request"), "/researches");
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const draft = useDraft(workspaceId ? originalDraftKey : null, {
    title: "", paperId: initialPaperId || papers[0]?.id || "", datasetId: datasets[0]?.id || "",
    taskText: JSON.stringify(initialTask || emptyTask(), null, 2), note: "",
  }, isResearchDraft);
  const { title, paperId, datasetId, taskText } = draft.value;
  const setTitle = (title: string) => draft.update((previous) => ({ ...previous, title }));
  const setPaperId = (paperId: string) => { draft.update((previous) => ({ ...previous, paperId })); setConfirmed(false); };
  const setDatasetId = (datasetId: string) => { draft.update((previous) => ({ ...previous, datasetId })); setConfirmed(false); };
  const setTaskText = (taskText: string) => { draft.update((previous) => ({ ...previous, taskText })); setConfirmed(false); };
  const { paper, error: paperError } = usePaper(paperId);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [paperTitle, setPaperTitle] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const dataset = datasets.find((value) => value.id === datasetId);
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
      if (!confirmed || !dataset || dataIssues.errors.length) throw new Error("请检查所选数据版本与时间切分，并确认后创建研究。");
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
        if (mounted.current) setSavedNotice(`研究已创建；本机记录尚未清理，原请求已保留，请安全重试确认。${describeError(e)}`);
        return;
      }
      if (mounted.current) {
        if (cleanupKey === originalDraftKey) draft.clear();
        setSavedNotice("研究已创建。");
        try { await onCreated(created); }
        catch (e) { if (mounted.current) setSavedNotice(`研究已创建，列表刷新暂未成功。${describeError(e)}`); }
      }
    } catch (e) { if (mounted.current) onError(describeError(e)); }
    finally { if (mounted.current) setSaving(false); }
  }
  return (
    <section className="panel create-panel">
      <div className="panel-heading">
        <div>
          <h2>从论文创建研究</h2>
        </div>
        <button className="button small" onClick={onCancel}>
          返回工作台
        </button>
      </div>
      {savedNotice && <p role="status">{savedNotice}</p>}
      {mutation.error && <p role="alert">{mutation.error}</p>}
      {mutation.pending && <section aria-label="研究创建恢复" className="info-note">
        <p>{mutation.pending.state === "rejected" ? "创建请求已明确拒绝，可以解除后修改。" : "研究创建结果待确认。安全重试原请求可以确认是否已创建，不会生成第二项研究。"}</p>
        <JsonDetails value={mutation.pending.body} label="查看冻结创建请求" />
        {mutation.pending.state === "rejected" ? <button className="button" disabled={!!busy || saving} onClick={mutation.dismissRejected}>解除被拒绝的创建请求</button> :
          <button className="button" disabled={!!busy || saving} onClick={() => void save()}>安全重试研究创建</button>}
      </section>}
      <DraftNotice pending={draft.pending} error={draft.error} enabled={!!workspaceId} onRestore={() => { draft.restore(); setConfirmed(false); }} onDiscard={() => { draft.discard(); setConfirmed(false); }} />
      <fieldset disabled={!!draft.pending || !draft.ready || !!busy || saving || !!mutation.pending || !mutation.ready || mutation.blocked} className="editor-container">
      <details>
        <summary>上传、验证并登记新的合成数据版本</summary>
        <DatasetImportPanel onRegistered={async (id) => {
          await onRefreshDatasets();
          setDatasetId(id);
          setConfirmed(false);
        }} />
      </details>
      <div className="create-grid">
        <div>
          <h3>1. 上传或选择论文</h3>
          <form className="upload-zone" onSubmit={upload}>
            <p>PDF 文件 · 保留原文与页码</p>
            <Field label="论文文件">
              <input
                type="file"
                accept="application/pdf,.pdf"
                onChange={(e) => setFile(e.target.files?.[0] || null)}
              />
            </Field>
            <Field label="论文标题">
              <input
                value={paperTitle}
                onChange={(e) => setPaperTitle(e.target.value)}
                placeholder="可选，默认使用文件名"
              />
            </Field>
            <button
              className="button"
              disabled={!file || uploading || !!busy}
              type="submit"
            >
              {uploading ? "上传与提取中…" : "上传论文"}
            </button>
          </form>
          <div className="info-note">
            AI
            接口留白。当前由你手动填写证据、研究假设和候选实现；字段与表达式由引擎校验。
          </div>
        </div>
        <form onSubmit={create}>
          <h3>2. 定义研究与候选</h3>
          <Field label="研究标题">
            <input
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="例如：价格与成交量关系研究"
            />
          </Field>
          <Field label="研究论文">
            <select
              required
              value={paperId}
              onChange={(e) => setPaperId(e.target.value)}
            >
              <option value="">选择已上传论文</option>
              {papers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))}
            </select>
          </Field>
          {paperId && (
            <a
              className="text-link"
              href={`/api/papers/${paperId}/pdf`}
              target="_blank"
              rel="noreferrer"
            >
              查看所选论文 ↗
            </a>
          )}
          <Field label="数据版本">
            <select
              required
              value={datasetId}
              onChange={(e) => { setDatasetId(e.target.value); setConfirmed(false); }}
            >
              <option value="">选择数据集</option>
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.version} · {d.data_kind === "synthetic" ? "合成数据" : d.data_kind} · {shortId(d.sha256)}
                </option>
              ))}
            </select>
          </Field>
          {dataset && <div className="info-note">
            <p>{dataset.title} · {dataset.version} · 摘要 {shortId(dataset.sha256)}</p>
            <p>一个研究固定绑定一个数据版本。换数据需创建新研究；原任务草稿不会自动改变。</p>
            <JsonDetails value={{ fields: dataset.fields, metadata: dataset.metadata, sha256: dataset.sha256 }} label="查看所选数据的日历、字段与完整元信息" />
          </div>}
          {paperError && <p className="info-note warning" role="alert">{paperError}</p>}
          <TaskEditor value={taskText} onChange={setTaskText} paper={paper} dataset={dataset} />
          {!!dataIssues.errors.length && <div className="info-note warning"><strong>需要调整任务配置</strong><ul>{dataIssues.errors.map((message) => <li key={message}>{message}</li>)}</ul></div>}
          {!!dataIssues.warnings.length && <div className="info-note"><ul>{dataIssues.warnings.map((message) => <li key={message}>{message}</li>)}</ul><p>负例仍可保留为研究任务；具体阻断记录由后端预检及实验生成。</p></div>}
          <Field label="已确认所选数据版本及 train / validation / test 时间切分">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
          </Field>
          <button
            className="button primary"
            disabled={
              !!busy || uploading || !paper || !dataset || !title.trim() || !confirmed || !!dataIssues.errors.length
            }
            type="submit"
          >
            创建研究
          </button>
        </form>
      </div>
      </fieldset>
    </section>
  );
}
