import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Run } from "../domain";
import type { FormulaAlignmentResponse, FormulaAlignmentTarget } from "../formulaAlignment";
import { assertFormulaAlignment, formulaAlignmentPath, formulaAlignmentTarget, formulaAlignmentTargetKey } from "../formulaAlignment";
import { useI18n } from "../i18n";
import { JsonDetails } from "./ui";
const REQUEST_TIMEOUT_MS = 15000;
export default function FormulaAlignment({run, candidateId}: {run: Run; candidateId: string}) {
  const target = formulaAlignmentTarget(run, candidateId), key = target ? formulaAlignmentTargetKey(target) : "unavailable";
  return <FormulaAlignmentReader key={key} target={target} run={run}/>;
}
function FormulaAlignmentReader({target, run}: {target: FormulaAlignmentTarget | null; run: Run}) {
  const {t} = useI18n();
  const key = target ? formulaAlignmentTargetKey(target) : "unavailable";
  const sequence = useRef(0), [activated, setActivated] = useState(false), [retry, setRetry] = useState(0);
  const [state, setState] = useState<{key: string; result?: FormulaAlignmentResponse; error?: "timeout" | "unavailable"}>({key});
  useEffect(() => {
    const epoch = ++sequence.current, controller = new AbortController(); let active = true, timedOut = false;
    setState({key});
    if (!target || !activated) return () => { active = false; controller.abort(); };
    const timer = setTimeout(() => { timedOut = true; controller.abort(); if (active && sequence.current === epoch) setState({key, error: "timeout"}); }, REQUEST_TIMEOUT_MS);
    api<unknown>(formulaAlignmentPath(target), {signal: controller.signal}).then(result => {
      assertFormulaAlignment(result, target, run);
      if (active && !timedOut && !controller.signal.aborted && sequence.current === epoch) setState({key, result});
    }).catch(() => { if (active && !timedOut && !controller.signal.aborted && sequence.current === epoch) setState({key, error: "unavailable"}); }).finally(() => clearTimeout(timer));
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  }, [key, activated, retry]);
  const current = state.key === key ? state : null;
  return <section className="formula-alignment" aria-label={t("公式结构与声明差异", "Formula structure and declared differences")} aria-busy={!!target && activated && !current?.result && !current?.error}>
    <details onToggle={event => { if (event.currentTarget.open) setActivated(true); }}><summary>{t("公式结构与声明差异", "Formula structure and declared differences")}</summary>
      <p className="fine-print">{t("只比较冻结公式的确定性结构；研究语义仍待评审，同指标不证明同公式。", "Only deterministic frozen formula structure is compared. Research semantics remain unassessed; equal metrics do not prove equal formulas.")}</p>
      {!target ? <p role="status">{t("完整冻结目标不可用，比较保持未知；计算输出仍可独立核对。", "The complete frozen target is unavailable; comparison remains unknown and computed output can be checked independently.")}</p>
        : current?.result ? <FormulaAlignmentView alignment={current.result}/>
          : current?.error ? <div role="status"><p>{current.error === "timeout" ? t("公式分析读取已超时；计算输出不受影响。", "Formula analysis loading timed out; computed output is unaffected.") : t("公式分析暂不可读取或身份不一致；计算输出不受影响。", "Formula analysis is unavailable or its identity differs; computed output is unaffected.")}</p><button type="button" onClick={() => setRetry(value => value + 1)}>{t("重新读取公式分析（只读）", "Reload formula analysis (read only)")}</button></div>
            : <p role="status">{activated ? t("正在读取当前冻结公式分析…", "Loading formula analysis for this frozen target…") : t("展开后读取本次候选，不调用模型。", "Expand to read this candidate; no model is called.")}</p>}
    </details>
  </section>;
}
export function FormulaAlignmentView({alignment: value}: {alignment: FormulaAlignmentResponse}) {
  const {t} = useI18n();
  const comparison = {exact: t("有限策略下结构一致", "Structure matches under the bounded policy"), different: t("结构不同", "Structure differs"), unknown: t("不可比较", "Not comparable")};
  const change = (value: boolean | null) => value === null ? t("未知", "Unknown") : value ? t("有变化", "Changed") : t("未发现变化", "No change detected");
  const list = (value?: string[] | null) => value ? value.length ? value.join(", ") : t("无", "None") : t("未知", "Unknown");
  const windows = (value: FormulaAlignmentResponse["features"]["reference"]) => value ? value.windows.length ? value.windows.map(item => `${item.operator}: ${item.rows} ` + t("行", "rows")).join("; ") : t("无行窗口", "No row window") : t("未知", "Unknown");
  const origins = {paper_original: t("论文原文（声明）", "Paper original (declared)"), user_modification: t("用户/项目修改（声明）", "User/project modification (declared)"), model_conjecture: t("模型推测（声明）", "Model conjecture (declared)")};
  const dimensions = {expression_legality: t("表达式合法性", "Expression legality"), formula_reference: t("公式参考", "Formula reference"), field_declarations: t("字段声明", "Field declarations"), execution_identity: t("执行公式一致性", "Execution formula identity"), attribution_declarations: t("归因声明", "Attribution declarations")};
  const toolStatus = {passed: t("工具检查通过", "Tool check passed"), failed: t("工具检查未通过", "Tool check failed"), warning: t("提示", "Warning"), unknown: t("未知", "Unknown")};
  const unassessed = {economic_mechanism: t("经济机制", "Economic mechanism"), natural_language_direction: t("文字信号方向", "Natural-language signal direction"), field_units: t("字段单位", "Field units"), calendar_and_missing_policy: t("交易日历与缺失规则", "Calendar and missing-data policy"), algebraic_equivalence: t("任意代数等价", "General algebraic equivalence"), overall_hypothesis_fidelity: t("整体假说忠实度", "Overall hypothesis fidelity")};
  const {features: f, field_declarations: fields} = value;
  return <div className="formula-alignment-record" data-formula-candidate={value.target.candidate_id} data-formula-semantic="unverified">
    <dl className="formula-comparisons">
      <div data-formula-comparison="reference_to_declared" data-comparison-status={value.reference_to_declared.status}><dt>{t("论文参考式 → 声明式", "Paper reference → declared expression")}</dt><dd>{comparison[value.reference_to_declared.status]} <code>{value.reference_to_declared.reason_code}</code></dd></div>
      <div data-formula-comparison="declared_to_executed" data-comparison-status={value.declared_to_executed.status}><dt>{t("声明式 → 实际执行式", "Declared → executed expression")}</dt><dd>{comparison[value.declared_to_executed.status]} <code>{value.declared_to_executed.reason_code}</code></dd></div>
    </dl>
    <p className="fine-print">{t("候选声明：", "Candidate declaration: ")}{origins[value.candidate.origin]} · {t("假说 ", "Hypothesis ")}{value.candidate.hypothesis_id}</p>
    {value.candidate.declared_changes.length > 0 && <ul className="formula-declared-changes" aria-label={t("冻结改动声明", "Frozen change declarations")}>{value.candidate.declared_changes.map((item, index) => <li key={index}>{item}</li>)}</ul>}
    {value.reference.status === "identified" ? <div className="formula-reference"><p>{t("唯一冻结参考式", "Unique frozen reference expression")} · {value.reference.mode}{value.reference.alpha_number != null && ` · Alpha#${value.reference.alpha_number}`}</p><code className="expression">{value.reference.expression}</code></div> : <p role="status">{t("论文参考未能唯一支持本次比较：", "The paper reference does not uniquely support this comparison: ")}{value.reference.status} · <code>{value.reference.reason_code}</code></p>}
    {value.reference.citations.length > 0 && <p className="fine-print">{t("参考引文（当前冻结实验）：", "Reference citations (this frozen run): ")}{value.reference.citations.map((item, index) => <span key={item.evidence_id}>{index > 0 && "; "}{item.evidence_id} · {t("PDF 物理页 ", "PDF physical page ")}{item.page}</span>)}</p>}
    {f.outer_transform && <p className="info-note formula-outer-transform">{f.outer_transform === "rank" ? t("声明式在完整参考式外增加横截面 rank；这是公式结构改变。即使评估器给出相同指标，也不能消除该改变。", "The declared expression adds cross-sectional rank around the complete reference. This changes formula structure even if the evaluator produces equal metrics.") : t("声明式对完整参考式取负；这是结构上的符号改变，不自动验证文字信号方向。", "The declared expression negates the complete reference. This is a structural sign change, not verification of natural-language direction.")}</p>}
    <details className="formula-features"><summary>{t("字段、算子与窗口差异（参考式 → 声明式）", "Fields, operators and windows (reference → declared)")}</summary>
      <div className="table-scroll"><table aria-label={t("参考与声明的结构特征", "Reference and declared structural features")}><thead><tr><th>{t("特征", "Feature")}</th><th>{t("参考式", "Reference")}</th><th>{t("声明式", "Declared")}</th></tr></thead><tbody>
        <tr><th scope="row">{t("字段", "Fields")}</th><td>{list(f.reference?.fields)}</td><td>{list(f.declared?.fields)}</td></tr>
        <tr><th scope="row">{t("算子（含重复出现）", "Operators (including repeated occurrences)")}</th><td>{list(f.reference?.operators)}</td><td>{list(f.declared?.operators)}</td></tr>
        <tr><th scope="row">{t("算子行窗口", "Operator row windows")}</th><td>{windows(f.reference)}</td><td>{windows(f.declared)}</td></tr>
        <tr><th scope="row">{t("规范化常数", "Normalized constants")}</th><td>{list(f.reference?.constants)}</td><td>{list(f.declared?.constants)}</td></tr>
      </tbody></table></div>
      <p>{t("新增 / 移除字段：", "Added / removed fields: ")}{list(f.added_fields)} / {list(f.removed_fields)}<br/>{t("新增 / 移除算子：", "Added / removed operators: ")}{list(f.added_operators)} / {list(f.removed_operators)}</p>
      <p>{t("结构：", "Structure: ")}{change(f.structure_changed)} · {t("窗口或其计算对象：", "Windows or their input expressions: ")}{change(f.window_changed)} · {t("常数：", "Constants: ")}{change(f.constants_changed)}</p>
      <p className="fine-print">{t("窗口表示每个资产序列上的回看行数，区别于训练/验证/测试日期区间；相同字段名不证明单位、可得时间或经济含义一致。", "Windows count lookback rows in each asset series, separately from training/validation/test dates. Matching field names do not establish equal units, availability times or economic meaning.")}</p>
      <p>{t("使用 / 所需字段：", "Used / required fields: ")}{list(fields.used)} / {list(fields.required)}<br/>{t("未声明使用字段：", "Used but undeclared: ")}{list(fields.undeclared)}<br/>{t("声明但未使用：", "Declared but unused: ")}{list(fields.declared_unused)}</p>
    </details>
    <p className="formula-semantic-note">{t("研究语义仍未评审；结构一致不表示经济机制或整体假说正确。此读取不会填写人工审核、计算语义分数或调用模型。", "Research semantics remain unassessed. Matching structure does not establish a correct economic mechanism or hypothesis. This read neither writes human reviews, computes a semantic score nor calls a model.")}</p>
    <details><summary>{t("仍待独立评审的内容", "Still requires independent assessment")}</summary><ul>{value.unassessed_semantics.map(item => <li key={item}>{unassessed[item]}</li>)}</ul></details>
    <details className="formula-tool-checks"><summary>{t("确定性工具检查（不等同人工五维评审）", "Deterministic tool checks (separate from five human review dimensions)")}</summary><ul>{value.checks.map(item => <li key={item.dimension}><strong>{dimensions[item.dimension]}: {toolStatus[item.status]}</strong> · <code>{item.code}</code><p>{item.message}</p></li>)}</ul></details>
    {value.public_projection_redacted && <p className="fine-print">{t("公开投影已脱敏；显示文本可能不同于私有原文，来源与目标摘要由服务器核验。", "The public projection is redacted. Displayed text may differ from private source text; the server verifies source and target digests.")}</p>}
    <JsonDetails value={value.execution} label={t("查看冻结执行配置", "View frozen execution configuration")}/>
    <JsonDetails value={{target: value.target, policy_version: value.policy_version, public_view_digest: value.public_view_digest}} label={t("查看公式分析身份与策略", "View formula analysis identity and policy")}/>
  </div>;
}
