import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { describeError } from "../domain";
import type { Dataset, Evidence, JsonObject, Paper, Task } from "../domain";
import { attributionNames, formTask, nextId, quoteMatches, taskDifferences, taskFormIssues, textLines } from "../taskEditor";
import { Field } from "./ui";

export function usePaper(paperId: string) {
  const [paper, setPaper] = useState<Paper | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setPaper(null); setError("");
    if (!paperId) return;
    const abort = new AbortController();
    void api<Paper>(`/papers/${encodeURIComponent(paperId)}`, { signal: abort.signal })
      .then((p) => { if (!abort.signal.aborted) setPaper(p); })
      .catch((e) => { if (!abort.signal.aborted) setError(describeError(e)); });
    return () => abort.abort();
  }, [paperId]);
  return { paper: paper?.id === paperId ? paper : null, error };
}

function Attribution({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <Field label={label}><select value={value} onChange={(e) => onChange(e.target.value)}>{Object.entries(attributionNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></Field>;
}

function EvidenceFields({ evidence, index, paper, onChange }: { evidence: Evidence; index: number; paper: Paper | null; onChange: (value: Evidence) => void }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [selectionError, setSelectionError] = useState("");
  const text = paper?.pages?.find((p) => p.page === evidence.page)?.text || "";
  return <>
    <Field label={`证据 ${index + 1} 页码`}><select value={evidence.page || ""} onChange={(e) => { onChange({ ...evidence, page: Number(e.target.value) }); setSelectionError(""); }}>
      <option value="">选择原文页码</option>{paper?.pages?.map((p) => <option key={p.page} value={p.page}>第 {p.page} 页</option>)}
    </select></Field>
    {text && <details><summary>查看此页提取原文并选择引用</summary>
      <textarea ref={ref} aria-label={`证据 ${index + 1} 页原文`} rows={9} value={text} readOnly />
      <button type="button" className="button small" onClick={() => {
        const quote = ref.current?.value.slice(ref.current.selectionStart, ref.current.selectionEnd) || "";
        if (!quote.trim()) { setSelectionError("请先在原文框中选中需要引用的文字。"); return; }
        onChange({ ...evidence, quote }); setSelectionError("");
      }}>采用选中的原文</button>
      {paper && <a className="text-link" href={`/api/papers/${paper.id}/pdf#page=${evidence.page}`} target="_blank" rel="noreferrer">打开第 {evidence.page} 页 PDF ↗</a>}
    </details>}
    {selectionError && <p role="alert">{selectionError}</p>}
    <Field label={`证据 ${index + 1} 引用原文`} hint="可从所选页复制或选择文字。字面匹配不等于论文支持你的解释。"><textarea rows={3} value={evidence.quote} onChange={(e) => onChange({ ...evidence, quote: e.target.value })} /></Field>
    {evidence.quote.trim() && paper && <p className={quoteMatches(evidence.quote, text) ? "fine-print" : "info-note warning"}>{quoteMatches(evidence.quote, text) ? "字面引用已匹配；语义忠实度仍需人工审核。" : "此引用未匹配所选页，请检查页码、原文及 PDF 提取情况。"}</p>}
  </>;
}

export default function TaskEditor({ value, onChange, paper, dataset, baseTask }: {
  value: string; onChange: (value: string) => void; paper: Paper | null; dataset?: Dataset; baseTask?: Task;
}) {
  let task: Task | null = null;
  let parseError = "";
  try { task = formTask(value); } catch (e) { parseError = describeError(e); }
  const update = (next: Task) => onChange(JSON.stringify(next, null, 2));
  const list = (kind: "evidence" | "hypotheses" | "candidates", index: number, patch: object) => {
    if (!task) return;
    update({ ...task, [kind]: task[kind].map((row, i) => i === index ? { ...row, ...patch } : row) });
  };
  const issues = task ? taskFormIssues(task, paper) : [parseError];
  const differences = task && baseTask ? taskDifferences(baseTask, task) : [];
  const splits = task?.evaluation.splits && typeof task.evaluation.splits === "object" ? task.evaluation.splits as JsonObject : {};
  const dates = Array.isArray(dataset?.metadata.calendar_dates) ? dataset.metadata.calendar_dates.filter((d): d is string => typeof d === "string") : [];
  return <div className="task-editor">
    {task && <>
      <h3>论文证据</h3>
      <p className="fine-print">引用、假设和候选的内部标识由系统生成。删除被引用内容前，先移除关联。</p>
      {task.evidence.map((e, index) => <fieldset key={e.id} className="task-editor-row"><legend>证据 {index + 1} · {e.id}</legend>
        <EvidenceFields evidence={e} index={index} paper={paper} onChange={(next) => list("evidence", index, next)} />
        <button type="button" className="button small" disabled={task!.hypotheses.some((h) => h.evidence_ids.includes(e.id))} onClick={() => update({ ...task!, evidence: task!.evidence.filter((x) => x.id !== e.id) })}>删除证据 {index + 1}</button>
      </fieldset>)}
      <button type="button" className="button small" disabled={!paper?.pages || task.evidence.length >= 100} onClick={() => update({ ...task!, evidence: [...task!.evidence, { id: nextId("evidence", task!.evidence), page: paper?.pages?.[0]?.page || 1, quote: "" }] })}>添加论文证据</button>
      <h3>研究假设</h3>
      {task.hypotheses.map((h, index) => <fieldset key={h.id} className="task-editor-row"><legend>假设 {index + 1} · {h.id}</legend>
        <Field label={`假设 ${index + 1} 研究主张`}><textarea rows={2} value={h.claim} onChange={(e) => list("hypotheses", index, { claim: e.target.value })} /></Field>
        <Attribution label={`假设 ${index + 1} 主张归属`} value={h.attribution} onChange={(attribution) => list("hypotheses", index, { attribution })} />
        <p>关联证据（至少一条）</p><div className="evidence-options">{task!.evidence.map((e, n) => <label key={e.id}><input type="checkbox" checked={h.evidence_ids.includes(e.id)} onChange={(event) => list("hypotheses", index, { evidence_ids: event.target.checked ? [...h.evidence_ids, e.id] : h.evidence_ids.filter((id) => id !== e.id) })} />假设 {index + 1} 引用证据 {n + 1} · 第 {e.page} 页 · {e.quote.slice(0, 60)}</label>)}</div>
        <Field label={`假设 ${index + 1} 经济机制`}><textarea rows={3} value={h.economic_mechanism} onChange={(e) => list("hypotheses", index, { economic_mechanism: e.target.value })} /></Field>
        <Attribution label={`假设 ${index + 1} 机制归属`} value={h.mechanism_attribution} onChange={(mechanism_attribution) => list("hypotheses", index, { mechanism_attribution })} />
        <Field label={`假设 ${index + 1} 信号方向`}><input value={h.signal_direction} onChange={(e) => list("hypotheses", index, { signal_direction: e.target.value })} /></Field>
        <Field label={`假设 ${index + 1} 所需数据字段`} hint={`每行一个字段；当前可用：${dataset?.fields.join(", ") || "请先选择数据"}。字段缺失会阻断候选，不自动替代。`}><textarea rows={3} value={h.required_fields.join("\n")} onChange={(e) => list("hypotheses", index, { required_fields: e.target.value.split("\n") })} onBlur={() => list("hypotheses", index, { required_fields: textLines(h.required_fields.join("\n")) })} /></Field>
        <Field label={`假设 ${index + 1} 适用条件与假设`} hint="每行一项。明确数据时点、当地执行约定、尚未验证的解释。"><textarea rows={4} value={h.assumptions.join("\n")} onChange={(e) => list("hypotheses", index, { assumptions: e.target.value.split("\n") })} onBlur={() => list("hypotheses", index, { assumptions: textLines(h.assumptions.join("\n")) })} /></Field>
        <button type="button" className="button small" disabled={task!.candidates.some((c) => c.hypothesis_id === h.id)} onClick={() => update({ ...task!, hypotheses: task!.hypotheses.filter((x) => x.id !== h.id) })}>删除假设 {index + 1}</button>
      </fieldset>)}
      <button type="button" className="button small" disabled={task.hypotheses.length >= 100} onClick={() => update({ ...task!, hypotheses: [...task!.hypotheses, { id: nextId("hypothesis", task!.hypotheses), claim: "", attribution: "user_modification", economic_mechanism: "", mechanism_attribution: "user_modification", signal_direction: "", evidence_ids: [], required_fields: [], assumptions: [] }] })}>添加研究假设</button>
      <h3>候选实现</h3>
      {task.candidates.map((c, index) => <fieldset key={c.id} className="task-editor-row"><legend>候选 {index + 1} · {c.id}</legend>
        <Field label={`候选 ${index + 1} 关联假设`}><select value={c.hypothesis_id} onChange={(e) => list("candidates", index, { hypothesis_id: e.target.value })}><option value="">选择研究假设</option>{task!.hypotheses.map((h, n) => <option key={h.id} value={h.id}>假设 {n + 1} · {h.claim.slice(0, 80)}</option>)}</select></Field>
        <Field label={`候选 ${index + 1} 表达式`} hint="使用本地引擎 DSL；字段、算子与参数由服务端预检，不能输入 Python 代码。"><textarea rows={3} className="code-editor" value={c.expression} onChange={(e) => list("candidates", index, { expression: e.target.value })} /></Field>
        <Attribution label={`候选 ${index + 1} 实现来源`} value={c.origin} onChange={(origin) => list("candidates", index, { origin })} />
        <Field label={`候选 ${index + 1} 改动说明`} hint="每行一项。若使用论文原式则留空；改动后必须更新来源。"><textarea rows={2} value={c.changes.join("\n")} onChange={(e) => list("candidates", index, { changes: e.target.value.split("\n") })} onBlur={() => list("candidates", index, { changes: textLines(c.changes.join("\n")) })} /></Field>
        <button type="button" className="button small" onClick={() => update({ ...task!, candidates: task!.candidates.filter((x) => x.id !== c.id) })}>删除候选 {index + 1}</button>
      </fieldset>)}
      <button type="button" className="button small" disabled={task.candidates.length >= 100} onClick={() => update({ ...task!, candidates: [...task!.candidates, { id: nextId("candidate", task!.candidates), hypothesis_id: "", expression: "", origin: "user_modification", changes: [] }] })}>添加候选实现</button>
      <h3>时间切分与运行预算</h3>
      <p className="fine-print">开发实验只使用验证区间；最终测试区间保持保留。系统不会自动修改切分。</p>
      {(["train", "validation", "test"] as const).map((name) => <div className="task-editor-dates" key={name}>{(["start", "end"] as const).map((edge) => {
        const bounds = splits[name] && typeof splits[name] === "object" ? splits[name] as JsonObject : {};
        return <Field key={edge} label={`${name} ${edge === "start" ? "开始" : "结束"}日期`}><input type="date" min={dates[0]} max={dates.at(-1)} value={typeof bounds[edge] === "string" ? bounds[edge] : ""} onChange={(e) => update({ ...task!, evaluation: { ...task!.evaluation, splits: { ...splits, [name]: { ...bounds, [edge]: e.target.value } } } })} /></Field>;
      })}</div>)}
      <Field label="最少有效资产数"><input type="number" min={3} step={1} value={Number(task.evaluation.min_assets ?? 3)} onChange={(e) => update({ ...task!, evaluation: { ...task!.evaluation, min_assets: Number(e.target.value) } })} /></Field>
      {([ ["max_candidates", "最多候选数", 20], ["max_attempts_per_candidate", "每个候选最多尝试次数", 5], ["max_tool_calls", "最多工具调用次数", 200], ["max_seconds", "最多运行秒数", 600] ] as const).map(([key, label, max]) => <Field key={key} label={label}><input type="number" min={key === "max_seconds" ? 0.1 : 1} max={max} step={key === "max_seconds" ? "any" : 1} value={Number(task!.budget[key] ?? "")} onChange={(e) => update({ ...task!, budget: { ...task!.budget, [key]: Number(e.target.value) } })} /></Field>)}
    </>}
    {!!issues.length && <div className="info-note warning" role="alert"><p>提交前需要调整：</p><ul>{issues.map((issue) => <li key={issue}>{issue}</li>)}</ul></div>}
    {baseTask && <details open className="task-diff"><summary>相对基础修订的修改（{differences.length} 项）</summary>{differences.length ? differences.map((diff) => <div key={diff.path}><strong>{diff.path}</strong><pre>之前：{JSON.stringify(diff.before) ?? "不存在"}{"\n"}之后：{JSON.stringify(diff.after) ?? "已删除"}</pre></div>) : <p>研究定义尚未改变。</p>}</details>}
    <details open={!!parseError} className="json-details"><summary>高级：直接编辑任务 JSON</summary><Field label="任务定义 JSON" hint="用于检查或导入已有定义；表单与 JSON 使用同一份数据。未知字段仍由服务端拒绝。"><textarea className="code-editor" rows={20} value={value} onChange={(e) => onChange(e.target.value)} spellCheck={false} /></Field></details>
  </div>;
}
