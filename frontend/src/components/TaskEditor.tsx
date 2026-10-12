import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { describeError } from "../domain";
import type { Dataset, Evidence, JsonObject, Paper, Task } from "../domain";
import { attributionNames, editEconomicMechanism, formTask, nextId, quoteMatches, taskDifferences, taskFormIssues, textLines } from "../taskEditor";
import type { TaskEditorSection } from "../taskEditor";
import { Field } from "./ui";
import { useI18n } from "../i18n";
import "./experimentFlow.css";

export function usePaper(paperId: string, scopeKey = "") {
  // A new identity also distinguishes A → B → A, before effects are committed.
  const scope = useMemo(() => ({ paperId, scopeKey }), [paperId, scopeKey]);
  const [result, setResult] = useState<{ scope: typeof scope; paper: Paper | null; error: string }>({ scope, paper: null, error: "" });
  useEffect(() => {
    setResult({ scope, paper: null, error: "" });
    if (!scope.paperId) return;
    const abort = new AbortController();
    void api<Paper>(`/papers/${encodeURIComponent(scope.paperId)}`, { signal: abort.signal })
      .then((paper) => { if (!abort.signal.aborted) setResult({ scope, paper, error: "" }); })
      .catch((error) => { if (!abort.signal.aborted) setResult({ scope, paper: null, error: describeError(error) }); });
    return () => abort.abort();
  }, [scope]);
  // Reset display synchronously; an old error must not flash in a new workspace.
  const current = result.scope === scope;
  return { paper: current && result.paper?.id === paperId ? result.paper : null, error: current ? result.error : "" };
}

function Attribution({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  const { t } = useI18n();
  const names: Record<string, string> = { paper_original: "Paper original", user_modification: "My modification / interpretation", model_conjecture: "Model conjecture (requires review)" };
  return <Field label={label}><select value={value} onChange={(e) => onChange(e.target.value)}>{Object.entries(attributionNames).map(([id, name]) => <option key={id} value={id}>{t(name, names[id] || id)}</option>)}</select></Field>;
}

function EvidenceFields({ evidence, index, paper, onChange }: { evidence: Evidence; index: number; paper: Paper | null; onChange: (value: Evidence) => void }) {
  const { t } = useI18n();
  const ref = useRef<HTMLTextAreaElement>(null);
  const [selectionError, setSelectionError] = useState("");
  const text = paper?.pages?.find((p) => p.page === evidence.page)?.text || "";
  return <>
    <Field label={t(`证据 ${index + 1} 页码`, `Evidence ${index + 1} page`)}><select value={evidence.page || ""} onChange={(e) => { onChange({ ...evidence, page: Number(e.target.value) }); setSelectionError(""); }}>
      <option value="">{t("选择原文页码", "Choose a source page")}</option>{paper?.pages?.map((p) => <option key={p.page} value={p.page}>{t(`第 ${p.page} 页`, `Page ${p.page}`)}</option>)}
    </select></Field>
    {text && <details><summary>{t("查看此页提取原文并选择引用", "Read extracted source text and select a quotation")}</summary>
      <textarea ref={ref} aria-label={t(`证据 ${index + 1} 页原文`, `Evidence ${index + 1} page source text`)} rows={9} value={text} readOnly />
      <button type="button" className="button small" onClick={() => {
        const quote = ref.current?.value.slice(ref.current.selectionStart, ref.current.selectionEnd) || "";
        if (!quote.trim()) { setSelectionError(t("请先在原文框中选中需要引用的文字。", "Select the text you want to quote in the source box first.")); return; }
        onChange({ ...evidence, quote }); setSelectionError("");
      }}>{t("采用选中的原文", "Use selected source text")}</button>
      {paper && <a className="text-link" href={`/api/papers/${paper.id}/pdf#page=${evidence.page}`} target="_blank" rel="noreferrer">{t(`打开第 ${evidence.page} 页 PDF ↗`, `Open PDF page ${evidence.page} ↗`)}</a>}
    </details>}
    {selectionError && <p role="alert">{selectionError}</p>}
    <Field label={t(`证据 ${index + 1} 引用原文`, `Evidence ${index + 1} quotation`)} hint={t("可从所选页复制或选择文字。字面匹配不等于论文支持你的解释。", "Copy or select text from the chosen page. A literal match does not establish support for your interpretation.")}><textarea rows={3} value={evidence.quote} onChange={(e) => onChange({ ...evidence, quote: e.target.value })} /></Field>
    {evidence.quote.trim() && paper && <p className={quoteMatches(evidence.quote, text) ? "fine-print" : "info-note warning"}>{quoteMatches(evidence.quote, text) ? t("字面引用已匹配；语义忠实度仍需人工审核。", "Literal quotation matched; semantic fidelity still requires human review.") : t("此引用未匹配所选页，请检查页码、原文及 PDF 提取情况。", "This quotation did not match the chosen page. Check the page number, source and PDF extraction.")}</p>}
  </>;
}

export default function TaskEditor({ value, onChange, paper, dataset, baseTask, section = "all", showIssues = true, configurationOnly = false }: {
  value: string; onChange: (value: string) => void; paper: Paper | null; dataset?: Dataset; baseTask?: Task; section?: TaskEditorSection; showIssues?: boolean; configurationOnly?: boolean;
}) {
  const { t } = useI18n();
  let task: Task | null = null;
  let parseError = "";
  try { task = formTask(value); } catch (e) { parseError = describeError(e); }
  const update = (next: Task) => onChange(JSON.stringify(next, null, 2));
  const list = (kind: "evidence" | "hypotheses" | "candidates", index: number, patch: object) => {
    if (!task) return;
    update({ ...task, [kind]: task[kind].map((row, i) => i === index ? { ...row, ...patch } : row) });
  };
  const issues = task ? taskFormIssues(task, paper, section) : [parseError];
  const differences = task && baseTask ? taskDifferences(baseTask, task) : [];
  const splits = task?.evaluation.splits && typeof task.evaluation.splits === "object" ? task.evaluation.splits as JsonObject : {};
  const declaredDates = dataset?.data_kind === "daily_development_snapshot" ? dataset.metadata.declared_calendar_dates : dataset?.metadata.calendar_dates;
  const dates = Array.isArray(declaredDates) ? declaredDates.filter((d): d is string => typeof d === "string") : [];
  return <div className="task-editor experiment-flow">
    {task && <>
      {(section === "all" || section === "evidence") && <>
      <h3>{t("论文证据", "Paper evidence")}</h3>
      <p className="fine-print">{t("引用、假设和候选的内部标识由系统生成。删除被引用内容前，先移除关联。", "Internal evidence, hypothesis and candidate identifiers are generated automatically. Remove links before deleting referenced items.")}</p>
      {task.evidence.map((e, index) => <fieldset key={e.id} className="task-editor-row"><legend>{t(`证据 ${index + 1}`, `Evidence ${index + 1}`)} <code>{e.id}</code></legend>
        <EvidenceFields evidence={e} index={index} paper={paper} onChange={(next) => list("evidence", index, next)} />
        <button type="button" className="button small" disabled={task!.hypotheses.some((h) => h.evidence_ids.includes(e.id))} onClick={() => update({ ...task!, evidence: task!.evidence.filter((x) => x.id !== e.id) })}>{t(`删除证据 ${index + 1}`, `Delete evidence ${index + 1}`)}</button>
      </fieldset>)}
      <button type="button" className="button small" disabled={!paper?.pages || task.evidence.length >= 100} onClick={() => update({ ...task!, evidence: [...task!.evidence, { id: nextId("evidence", task!.evidence), page: paper?.pages?.[0]?.page || 1, quote: "" }] })}>{t("添加论文证据", "Add paper evidence")}</button>
      <h3>{t("研究假设", "Research hypotheses")}</h3>
      {task.hypotheses.map((h, index) => <fieldset key={h.id} className="task-editor-row"><legend>{t(`假设 ${index + 1}`, `Hypothesis ${index + 1}`)} <code>{h.id}</code></legend>
        <Field label={t(`假设 ${index + 1} 研究主张`, `Hypothesis ${index + 1} claim`)}><textarea rows={2} value={h.claim} onChange={(e) => list("hypotheses", index, { claim: e.target.value })} /></Field>
        <Attribution label={t(`假设 ${index + 1} 主张归属`, `Hypothesis ${index + 1} claim attribution`)} value={h.attribution} onChange={(attribution) => list("hypotheses", index, { attribution })} />
        <p>{t("关联证据（至少一条）", "Linked evidence (at least one item)")}</p><div className="evidence-options">{task!.evidence.map((e, n) => <label key={e.id}><input type="checkbox" checked={h.evidence_ids.includes(e.id)} onChange={(event) => list("hypotheses", index, { evidence_ids: event.target.checked ? [...h.evidence_ids, e.id] : h.evidence_ids.filter((id) => id !== e.id) })} />{t(`假设 ${index + 1} 引用证据 ${n + 1} · 第 ${e.page} 页`, `Hypothesis ${index + 1} cites evidence ${n + 1} · page ${e.page}`)} · {e.quote.slice(0, 60)}</label>)}</div>
        <Field label={t(`假设 ${index + 1} 经济机制`, `Hypothesis ${index + 1} economic mechanism`)}><textarea rows={3} value={h.economic_mechanism ?? ""} placeholder={h.economic_mechanism === null ? t("未提供 / 未决；仅公式工程验证", "Not provided / unresolved; formula engineering verification only") : undefined} onChange={(e) => list("hypotheses", index, editEconomicMechanism(h, e.target.value))} /></Field>
        {h.economic_mechanism === null ? <p>{t("机制归属：未决。保存继续保留 null，不补写解释；填写机制将作为用户修改进入普通研究分支。", "Mechanism attribution: unresolved. Saving preserves null without supplying an explanation; entering a mechanism creates a user modification in the ordinary research branch.")}</p> : <Attribution label={t(`假设 ${index + 1} 机制归属`, `Hypothesis ${index + 1} mechanism attribution`)} value={h.mechanism_attribution} onChange={(mechanism_attribution) => list("hypotheses", index, { mechanism_attribution })} />}
        <Field label={t(`假设 ${index + 1} 信号方向`, `Hypothesis ${index + 1} signal direction`)}><input value={h.signal_direction} onChange={(e) => list("hypotheses", index, { signal_direction: e.target.value })} /></Field>
        <Field label={t(`假设 ${index + 1} 所需数据字段`, `Hypothesis ${index + 1} required fields`)} hint={t(`每行一个字段；当前可用：${dataset?.fields.join(", ") || "请先选择数据"}。字段缺失会阻断候选，不自动替代。`, `One field per line. Available: ${dataset?.fields.join(", ") || "select a dataset first"}. Missing fields block candidates and are never substituted.`)}><textarea rows={3} value={h.required_fields.join("\n")} onChange={(e) => list("hypotheses", index, { required_fields: e.target.value.split("\n") })} onBlur={() => list("hypotheses", index, { required_fields: textLines(h.required_fields.join("\n")) })} /></Field>
        <Field label={t(`假设 ${index + 1} 适用条件与假设`, `Hypothesis ${index + 1} conditions and assumptions`)} hint={t("每行一项。明确数据时点、当地执行约定、尚未验证的解释。", "One item per line. State data timing, local execution conventions and unverified interpretations.")}><textarea rows={4} value={h.assumptions.join("\n")} onChange={(e) => list("hypotheses", index, { assumptions: e.target.value.split("\n") })} onBlur={() => list("hypotheses", index, { assumptions: textLines(h.assumptions.join("\n")) })} /></Field>
        <button type="button" className="button small" disabled={task!.candidates.some((c) => c.hypothesis_id === h.id)} onClick={() => update({ ...task!, hypotheses: task!.hypotheses.filter((x) => x.id !== h.id) })}>{t(`删除假设 ${index + 1}`, `Delete hypothesis ${index + 1}`)}</button>
      </fieldset>)}
      <button type="button" className="button small" disabled={task.hypotheses.length >= 100} onClick={() => update({ ...task!, hypotheses: [...task!.hypotheses, { id: nextId("hypothesis", task!.hypotheses), claim: "", attribution: "user_modification", economic_mechanism: "", mechanism_attribution: "user_modification", signal_direction: "", evidence_ids: [], required_fields: [], assumptions: [] }] })}>{t("添加研究假设", "Add research hypothesis")}</button>
      </>}
      {(section === "all" || section === "candidates") && <>
      <h3>{t("候选实现", "Candidate implementations")}</h3>
      {task.candidates.map((c, index) => <fieldset key={c.id} className="task-editor-row"><legend>{t(`候选 ${index + 1}`, `Candidate ${index + 1}`)} <code>{c.id}</code></legend>
        <Field label={t(`候选 ${index + 1} 关联假设`, `Candidate ${index + 1} linked hypothesis`)}><select value={c.hypothesis_id} onChange={(e) => list("candidates", index, { hypothesis_id: e.target.value })}><option value="">{t("选择研究假设", "Choose a research hypothesis")}</option>{task!.hypotheses.map((h, n) => <option key={h.id} value={h.id}>{t(`假设 ${n + 1}`, `Hypothesis ${n + 1}`)} · {h.claim.slice(0, 80)}</option>)}</select></Field>
        <Field label={t(`候选 ${index + 1} 表达式`, `Candidate ${index + 1} expression`)} hint={t("使用本地引擎 DSL；字段、算子与参数由服务端预检，不能输入 Python 代码。", "Use the local engine DSL. The server checks fields, operators and parameters; Python code is not supported.")}><textarea rows={3} className="code-editor" value={c.expression} onChange={(e) => list("candidates", index, { expression: e.target.value })} /></Field>
        <Attribution label={t(`候选 ${index + 1} 实现来源`, `Candidate ${index + 1} implementation attribution`)} value={c.origin} onChange={(origin) => list("candidates", index, { origin })} />
        <Field label={t(`候选 ${index + 1} 改动说明`, `Candidate ${index + 1} changes`)} hint={t("每行一项。若使用论文原式则留空；改动后必须更新来源。", "One item per line. Leave empty for the paper original; update attribution when making changes.")}><textarea rows={2} value={c.changes.join("\n")} onChange={(e) => list("candidates", index, { changes: e.target.value.split("\n") })} onBlur={() => list("candidates", index, { changes: textLines(c.changes.join("\n")) })} /></Field>
        <button type="button" className="button small" onClick={() => update({ ...task!, candidates: task!.candidates.filter((x) => x.id !== c.id) })}>{t(`删除候选 ${index + 1}`, `Delete candidate ${index + 1}`)}</button>
      </fieldset>)}
      <button type="button" className="button small" disabled={task.candidates.length >= 100} onClick={() => update({ ...task!, candidates: [...task!.candidates, { id: nextId("candidate", task!.candidates), hypothesis_id: "", expression: "", origin: "user_modification", changes: [] }] })}>{t("添加候选实现", "Add candidate implementation")}</button>
      </>}
      {(section === "all" || section === "experiment") && <>
      <h3>{configurationOnly ? t("时间切分", "Time windows") : t("时间切分与运行预算", "Time windows and execution budget")}</h3>
      <p className="fine-print">{t("开发实验只使用验证区间；最终测试区间保持保留。系统不会自动修改切分。", "Development experiments use the validation window only. The final test window remains held out. The system never changes your splits automatically.")}</p>
      {dates.length > 0 && <p className="flow-calendar">{t("所选数据日历", "Selected dataset calendar")}: <time>{dates[0]}</time> — <time>{dates.at(-1)}</time>{t(` · ${dates.length} 个交易日`, ` · ${dates.length} sessions`)}</p>}
      <div className="flow-split-editor">
      {(["train", "validation", "test"] as const).map((name) => {
        const bounds = objectValue(splits[name]);
        const start = typeof bounds.start === "string" ? bounds.start : "";
        const end = typeof bounds.end === "string" ? bounds.end : "";
        const invalid = !!start && !!end && (start > end || (dates.length > 0 && (!dates.includes(start) || !dates.includes(end))));
        return <fieldset className={`flow-window flow-window-${name}`} key={name}>
          <legend>{splitLabel(name, t)}</legend>
          <p className="fine-print">{splitPurpose(name, t)}</p>
          <div className="task-editor-dates">{(["start", "end"] as const).map((edge) =>
            <Field key={edge} label={t(`${name} ${edge === "start" ? "开始" : "结束"}日期`, `${name} ${edge} date`)}>
              <input type="date" min={dates[0]} max={dates.at(-1)} aria-invalid={invalid || undefined} value={typeof bounds[edge] === "string" ? bounds[edge] : ""} onChange={(e) => update({ ...task!, evaluation: { ...task!.evaluation, splits: { ...splits, [name]: { ...bounds, [edge]: e.target.value } } } })} />
            </Field>)}</div>
          {invalid && <p className="flow-error" role="alert">{t("请使用数据日历中的日期，结束日期不能早于开始日期。", "Use dates from the dataset calendar; the end cannot precede the start.")}</p>}
        </fieldset>;
      })}
      </div>
      <ExecutionSemantics />
      <div className="flow-budget-grid">
      <Field label={t("最少有效资产数", "Minimum valid assets")} hint={t("仅统计信号为有限数值的资产；不足时跳过，不补零。", "Only finite signal observations count. Insufficient observations are skipped, never filled with zero.")}><input type="number" min={3} step={1} value={Number(task.evaluation.min_assets ?? 3)} onChange={(e) => update({ ...task!, evaluation: { ...task!.evaluation, min_assets: Number(e.target.value) } })} /></Field>
      {!configurationOnly && ([ ["max_candidates", "最多候选数", "Maximum candidates", 20], ["max_attempts_per_candidate", "每个候选最多尝试次数", "Maximum attempts per candidate", 5], ["max_tool_calls", "最多工具调用次数", "Maximum tool calls", 200], ["max_seconds", "最多运行秒数", "Maximum runtime (seconds)", 600] ] as const).map(([key, zh, en, max]) => <Field key={key} label={t(zh, en)}><input type="number" min={key === "max_seconds" ? 0.1 : 1} max={max} step={key === "max_seconds" ? "any" : 1} value={Number(task!.budget[key] ?? "")} onChange={(e) => update({ ...task!, budget: { ...task!.budget, [key]: Number(e.target.value) } })} /></Field>)}
      </div>
      </>}
    </>}
    {showIssues && !!issues.length && <div className="info-note warning" role="alert"><p>{t("提交前需要调整：", "Resolve before submitting:")}</p><ul>{issues.map((issue) => <li key={issue}>{localizeTaskIssue(issue, t)}</li>)}</ul></div>}
    {baseTask && <details className="task-diff"><summary>{t(`相对基础修订的修改（${differences.length} 项）`, `Changes from the base revision (${differences.length})`)}</summary>{differences.length ? differences.map((diff) => <div key={diff.path}><strong>{diff.path}</strong><pre>{t("之前：", "Before: ")}{JSON.stringify(diff.before) ?? t("不存在", "Absent")}{"\n"}{t("之后：", "After: ")}{JSON.stringify(diff.after) ?? t("已删除", "Removed")}</pre></div>) : <p>{t("研究定义尚未改变。", "The research definition is unchanged.")}</p>}</details>}
    {!configurationOnly && <details open={!!parseError} className="json-details"><summary>{t("高级：直接编辑任务 JSON", "Advanced: edit task JSON")}</summary><Field label={t("任务定义 JSON", "Task definition JSON")} hint={t("用于检查或导入已有定义；表单与 JSON 使用同一份数据。未知字段仍由服务端拒绝。", "Inspect or import an existing definition. The form and JSON share the same data. The server rejects unknown fields.")}><textarea className="code-editor" rows={20} value={value} onChange={(e) => onChange(e.target.value)} spellCheck={false} /></Field></details>}
  </div>;
}


type Translator = (zh: string, en: string) => string;
const objectValue = (value: unknown): JsonObject => value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
function splitLabel(name: "train" | "validation" | "test", t: Translator) {
  return name === "train" ? t("训练区间 · train", "Training window · train") : name === "validation" ? t("验证区间 · validation", "Validation window · validation") : t("保留测试区间 · test", "Held-out test window · test");
}
function splitPurpose(name: "train" | "validation" | "test", t: Translator) {
  return name === "train" ? t("用于研究开发；本次实验不在此区间评估。", "For research development; this run does not evaluate this window.") : name === "validation" ? t("本次开发实验的评估区间。", "The evaluation window for this development run.") : t("保留给最终测试；本次不计算此区间的因子或指标。原数据文件仍会完整载入并检查。", "Reserved for final testing; this run computes no factors or metrics for this window. The full data file is still loaded and checked.");
}
/** Matches the current daily evaluator contract in paper_alpha/evaluation.py. */
export function ExecutionSemantics() {
  const { t } = useI18n();
  return <section className="flow-execution" aria-label={t("本地日频执行约定", "Local daily execution conventions")}>
    <h4>{t("信号与执行时点", "Signal and execution timing")}</h4>
    <ol className="flow-timing">
      <li><strong>{t("t 日收盘后", "After session t close")}</strong><span>{t("形成信号", "Form the signal")}</span></li>
      <li><strong>{t("t+1 日开盘", "Session t+1 open")}</strong><span>{t("入场", "Enter")}</span></li>
      <li><strong>{t("t+2 日开盘", "Session t+2 open")}</strong><span>{t("退出", "Exit")}</span></li>
    </ol>
    <p><code>open[t+2] / open[t+1] - 1</code></p>
    <p className="fine-print">{t("完整载入用于数据检查，不代表使用测试区间生成信号。当前日频 Provider 上下文不包含行情行、最终测试区间边界或结果。", "Loading the complete file for data checks does not mean using test observations to form signals. The current daily Provider context excludes market rows, final-test bounds and results.")}</p>
    <p className="fine-print">{t("信号、入场与退出必须全部处于验证区间；跨越边界的观测剔除。横截面排序组合的收益为毛收益，未扣成本、滑点、借券等。", "Signal, entry and exit must all lie inside validation; observations crossing its boundary are purged. Returns use a cross-sectional rank portfolio and are gross of costs, slippage and borrowing.")}</p>
  </section>;
}

export function TaskConfigurationSummary({ task, dataset }: { task: Task; dataset?: Dataset }) {
  const { t } = useI18n();
  const splits = objectValue(task.evaluation.splits);
  return <div className="flow-configuration">
    <dl className="flow-summary">
      {dataset && <div><dt>{t("固定数据版本", "Fixed data version")}</dt><dd>{dataset.version} · {dataset.data_kind === "synthetic" ? t("合成数据", "Synthetic data") : dataset.data_kind}<small>{dataset.title}</small><code>{dataset.sha256}</code></dd></div>}
      <div><dt>{t("候选集合", "Candidate set")}</dt><dd>{t(`全部 ${task.candidates.length} 个候选`, `All ${task.candidates.length} candidates`)}<small>{task.candidates.map((candidate) => candidate.id).join(" · ") || t("尚无候选", "No candidates yet")}</small></dd></div>
      <div><dt>{t("最少有效资产数", "Minimum valid assets")}</dt><dd>{String(task.evaluation.min_assets ?? 3)}</dd></div>
    </dl>
    <div className="flow-saved-windows">{(["train", "validation", "test"] as const).map((name) => {
      const bounds = objectValue(splits[name]);
      return <section key={name}><h4>{splitLabel(name, t)}</h4><p><time>{typeof bounds.start === "string" && bounds.start ? bounds.start : t("未设置", "Not set")}</time> — <time>{typeof bounds.end === "string" && bounds.end ? bounds.end : t("未设置", "Not set")}</time></p><p className="fine-print">{splitPurpose(name, t)}</p></section>;
    })}</div>
    <ExecutionSemantics />
  </div>;
}

/** Translate known form diagnostics only; opaque server errors and source text remain intact. */
export function localizeTaskIssue(message: string, t: Translator): string {
  const exact: Record<string, string> = {
    "所选版本缺少服务端确认的日历或资产集合，请刷新数据列表。": "The selected version has no server-confirmed calendar or universe. Refresh the dataset list.",
    "请填写 train、validation、test 时间切分。": "Enter train, validation and test time windows.",
    "max_seconds 需要大于 0 且不超过 600。": "max_seconds must be greater than 0 and at most 600.",
    "固定数据版本尚未加载。": "The fixed dataset version has not loaded yet.",
    "任务 JSON 格式有误，请检查逗号、引号和括号。": "Invalid task JSON. Check commas, quotation marks and brackets.",
    "任务必须是一个 JSON 对象。": "The task must be a JSON object.",
  };
  if (exact[message]) return t(message, exact[message]);
  const rules: [RegExp, (...parts: string[]) => string][] = [
    [/^任务缺少数组字段：(.+)$/, (key) => `The task is missing an array field: ${key}`],
    [/^任务缺少对象字段：(.+)$/, (key) => `The task is missing an object field: ${key}`],
    [/^请移除任务中的路径或未支持字段：(.+)。论文和数据由工作台选择。$/, (keys) => `Remove task paths or unsupported fields: ${keys}. Choose the paper and dataset in the workbench.`],
    [/^(train|validation|test) 的起止日期必须来自所选数据日历。$/, (name) => `${name} start and end dates must belong to the selected dataset calendar.`],
    [/^(train|validation|test) 至少包含 3 个交易日，且各区间必须依次排列、不重叠。$/, (name) => `${name} must contain at least 3 sessions. Windows must be chronological and non-overlapping.`],
    [/^min_assets 必须在 3 到 (\d+) 之间。$/, (max) => `min_assets must be between 3 and ${max}.`],
    [/^所需字段缺失：(.+)。依赖这些字段的候选将被阻断；系统不会替换字段。$/, (fields) => `Required fields are missing: ${fields}. Dependent candidates will be blocked; fields are never substituted.`],
    [/^(max_\w+) 需要为 1–(\d+) 的整数。$/, (key, max) => `${key} must be an integer between 1 and ${max}.`],
    [/^(证据|假设|候选)需要 1–100 条。$/, (name) => `${entityName(name)} requires 1–100 items.`],
    [/^(证据|假设|候选)存在重复标识。$/, (name) => `${entityName(name)} contains duplicate identifiers.`],
    [/^(证据|假设|候选)标识仅可包含 1–64 个英文字母、数字、下划线或连字符。$/, (name) => `${entityName(name)} identifiers must contain 1–64 letters, digits, underscores or hyphens.`],
    [/^证据 (\d+)：请选择页码并填写原文。$/, (n) => `Evidence ${n}: choose a page and enter the quotation.`],
    [/^证据 (\d+)：引用未在所选页提取文本中匹配，请核对 PDF。$/, (n) => `Evidence ${n}: the quotation did not match the extracted page. Check the PDF.`],
    [/^假设 (\d+)：请填写主张、经济机制和信号方向。$/, (n) => `Hypothesis ${n}: enter the claim, economic mechanism and signal direction.`],
    [/^假设 (\d+)：请选择主张及机制归属。$/, (n) => `Hypothesis ${n}: choose claim and mechanism attribution.`],
    [/^假设 (\d+)：请关联至少一条现存证据。$/, (n) => `Hypothesis ${n}: link at least one existing evidence item.`],
    [/^候选 (\d+)：请填写表达式并关联假设。$/, (n) => `Candidate ${n}: enter an expression and link a hypothesis.`],
    [/^候选 (\d+)：请选择实现来源。$/, (n) => `Candidate ${n}: choose implementation attribution.`],
    [/^候选 (\d+)：论文原式不应记录改动，修改或推测必须描述改动。$/, (n) => `Candidate ${n}: a paper-original expression must have no changes; modifications and conjectures must describe their changes.`],
    [/^(.+) 包含无效对象。$/, (name) => `${name} contains an invalid object.`],
    [/^(.+) 必须为文本。$/, (name) => `${name} must be text.`],
    [/^(.+) 必须为文本数组。$/, (name) => `${name} must be an array of text.`],
  ];
  for (const [pattern, translate] of rules) { const match = message.match(pattern); if (match) return t(message, translate(...match.slice(1))); }
  return message;
}
function entityName(name: string) { return name === "证据" ? "Evidence" : name === "假设" ? "Hypotheses" : "Candidates"; }
