import { useState } from "react";
import type { Candidate, Dataset, Hypothesis, Paper, Research, Revision } from "../domain";
import { useI18n } from "../i18n";
import { quoteMatches } from "../taskEditor";
import { JsonDetails } from "./ui";
import type { DailyStep, ResearchStage } from "./DailyWorkflow";
import "./researchStages.css";

type Props = {
  stage: ResearchStage; research: Research; revision: Revision; paper?: Paper; dataset?: Dataset;
  onStep: (step: DailyStep) => void; onEdit: () => void;
};
type PreflightCheck = NonNullable<Research["preflight"]>["candidates"][number];
type Translate = (zh: string, en: string) => string;
function originName(value: string, t: Translate) {
  const names: Record<string, string> = {
    paper_original: t("论文原文", "Paper source"),
    user_modification: t("用户修改 / 解释", "User modification / interpretation"),
    model_conjecture: t("模型推测", "Model conjecture"),
    unresolved: t("未决", "Unresolved"),
  };
  return names[value] || value;
}
function checkLabel(check: PreflightCheck | undefined, t: Translate) {
  if (!check) return t("未取得本版本预检", "No preflight for this version");
  if (check.status === "ready") return t("静态预检通过", "Static preflight passed");
  if (check.status === "blocked") return t("候选将被阻断", "Candidate will be blocked");
  if (check.status === "warning") return t("需检查执行条件", "Check execution requirements");
  return check.status;
}
function RequiredFields({ fields, dataset }: { fields: string[]; dataset?: Dataset }) {
  const { t } = useI18n();
  return <div className="rs-fields">{fields.map(field => {
    const missing = dataset && !dataset.fields.includes(field);
    return <code className={missing ? "missing" : undefined} key={field}>{field}{missing ? t(" · 缺失", " · missing") : ""}</code>;
  })}{!fields.length && <span className="rs-caption">{t("未声明字段依赖", "No field requirements declared")}</span>}</div>;
}
/** Selection is a reading aid, scoped to the saved research version. */
export default function ResearchStageContent(props: Props) {
  return <ResearchStageView key={`${props.research.id}:${props.revision.id}`} {...props} />;
}
function ResearchStageView({ stage, research, revision, paper: loadedPaper, dataset: loadedDataset, onStep, onEdit }: Props) {
  const { t } = useI18n();
  const task = revision.task;
  const paper = loadedPaper?.id === research.paper_id ? loadedPaper : undefined;
  const dataset = loadedDataset?.id === research.dataset_id ? loadedDataset : undefined;
  const [hypothesisId, setHypothesisId] = useState(task.hypotheses[0]?.id || "");
  const [candidateId, setCandidateId] = useState(task.candidates[0]?.id || "");
  const hypothesis = task.hypotheses.find(item => item.id === hypothesisId) || task.hypotheses[0];
  const candidate = task.candidates.find(item => item.id === candidateId) || task.candidates[0];
  const candidateHypothesis = task.hypotheses.find(item => item.id === candidate?.hypothesis_id);
  const preflight = revision.id === research.latest_revision_id && (!dataset || research.preflight?.dataset_version === dataset.version)
    ? research.preflight : undefined;
  const candidateCheck = preflight?.candidates.find(item => item.candidate_id === candidate?.id);
  const requiredFields = [...new Set(task.hypotheses.flatMap(item => item.required_fields))];
  const missingFields = dataset ? requiredFields.filter(field => !dataset.fields.includes(field)) : [];
  const pdfUrl = `/api/papers/${encodeURIComponent(research.paper_id)}/pdf`;
  const selectHypothesis = (item: Hypothesis) => {
    setHypothesisId(item.id);
    const linked = task.candidates.find(row => row.hypothesis_id === item.id);
    if (linked) setCandidateId(linked.id);
  };
  const selectCandidate = (item: Candidate) => {
    setCandidateId(item.id);
    setHypothesisId(item.hypothesis_id);
  };
  const heading = (title: string, description: string, editable = true) => <header className="rs-heading">
    <div><h2>{title}</h2><p>{description}</p></div>
    {editable && <button className="button small" onClick={onEdit}>{t("创建新版本", "Create new version")}</button>}
  </header>;
  const next = (description: string, label: string, step: DailyStep, disabled = false) => <footer className="rs-decision">
    <p>{description}</p><button className="button primary" disabled={disabled} onClick={() => onStep(step)}>{label}</button>
  </footer>;
  if (stage === "materials") return <section className="research-stage" aria-label={t("材料输入", "Source material")}>
    {heading(t("确认研究的起点", "Confirm your research starting point"), t("核对已登记的论文、可用内容与绑定的数据版本，再进入证据分析。", "Check the registered paper, available content and bound dataset before examining the evidence."), false)}
    <div className="rs-material-grid">
      <article className="panel rs-material">
        <div className="rs-section-top"><h3>{paper?.title || t("已登记论文", "Registered paper")}</h3><span className="rs-tag">{t("PDF 材料", "PDF source")}</span></div>
        <p className="rs-caption">{t("论文登记与假说成立是不同事项。原文与研究解释将在下一阶段逐条对照。", "Registering a paper does not validate a hypothesis. Compare source text and research interpretations in the next stage.")}</p>
        <dl className="rs-meta"><div><dt>{t("论文标识", "Paper ID")}</dt><dd><code>{research.paper_id}</code></dd></div><div><dt>{t("提取文本", "Extracted text")}</dt><dd>{paper?.pages ? t(`已载入 ${paper.pages.length} 页`, `${paper.pages.length} pages loaded`) : t("尚未载入页级文本，可打开 PDF 核对", "Page text is not loaded; inspect the PDF")}</dd></div></dl>
        <a className="text-link" href={pdfUrl} target="_blank" rel="noreferrer">{t("查看论文原文", "Open source PDF")}</a>
        {task.evidence.length > 0 && <details className="rs-source-excerpts"><summary>{t(`查看本版本的 ${task.evidence.length} 条引用`, `View ${task.evidence.length} citations in this version`)}</summary>{task.evidence.map(evidence => <div key={evidence.id}><a href={`${pdfUrl}#page=${evidence.page}`} target="_blank" rel="noreferrer">{t(`第 ${evidence.page} 页`, `Page ${evidence.page}`)}</a><blockquote>{evidence.quote}</blockquote></div>)}</details>}
        {paper && <JsonDetails value={{ paper_id: paper.id, sha256: paper.sha256 }} label={t("查看论文文件身份", "View paper file identity")} />}
      </article>
      <article className="panel rs-material">
        <div className="rs-section-top"><h3>{t("绑定的研究数据", "Bound research dataset")}</h3>{dataset?.data_kind === "synthetic" && <span className="rs-tag warning">{t("合成数据", "Synthetic data")}</span>}</div>
        <p className="rs-dataset-title">{dataset?.title || research.dataset_id}</p>
        <p className="rs-caption">{t("数据版本：", "Dataset version: ")}{dataset?.version || t("待读取", "Not loaded")}</p>
        <h4>{t("可用字段", "Available fields")}</h4>
        {dataset ? <RequiredFields fields={dataset.fields} /> : <p className="rs-caption">{t("尚未载入数据字段，不能确认是否满足候选条件。", "Dataset fields are not loaded, so candidate requirements cannot yet be checked.")}</p>}
        {missingFields.length > 0 && <div className="rs-data-gap"><strong>{t("本版本有未满足的数据条件", "This version has unmet data requirements")}</strong><p>{t(`缺少 ${missingFields.join(", ")}；保留缺字段原因，不隐式替换。`, `Missing ${missingFields.join(", ")}; preserve the reason and do not substitute fields implicitly.`)}</p></div>}
        {dataset?.data_kind === "synthetic" && <p className="rs-caption">{t("合成数据用于软件验证，不构成真实市场表现或投资有效性的证据。", "Synthetic data supports software validation. It is not evidence of real-market performance or investment merit.")}</p>}
        {dataset && <JsonDetails value={{ dataset_id: dataset.id, version: dataset.version, sha256: dataset.sha256, metadata: dataset.metadata }} label={t("查看数据版本与来源", "View dataset version and provenance")} />}
        <p className="rs-caption">{t("一个研究固定绑定一个数据版本。换数据需新建研究，历史结果保留。", "A study binds to one dataset version. Create a new study to change data; historical results remain available.")}</p>
      </article>
    </div>
    {next(t("原文出处与数据版本将贯穿假说、候选和结果审核。", "Source citations and the dataset version stay attached to hypotheses, candidates and result review."), t("继续查看证据与假说", "Examine evidence and hypotheses"), "evidence")}
  </section>;
  if (stage === "evidence") return <section className="research-stage" aria-label={t("证据与假说", "Evidence and hypotheses")}>
    {heading(t("从原文到研究假说", "From source text to research hypothesis"), t("先判断引用能支持什么，再区分原文主张、机制推测与自己的改动。", "Decide what the citation supports, then distinguish source claims, inferred mechanisms and your own edits."))}
    {hypothesis ? <div className="rs-evidence-layout">
      <aside className="rs-hypothesis-list" aria-label={t("选择假说", "Select a hypothesis")}>
        <p className="rs-list-count">{t(`${task.hypotheses.length} 条假说 · v${revision.number}`, `${task.hypotheses.length} hypotheses · v${revision.number}`)}</p>
        {task.hypotheses.map(item => <button key={item.id} className={`rs-hypothesis-choice ${item.id === hypothesis.id ? "selected" : ""}`} aria-pressed={item.id === hypothesis.id} onClick={() => selectHypothesis(item)}>
          <strong>{item.id}</strong><span className="rs-claim-preview">{item.claim}</span><small>{t(`${item.evidence_ids.length} 条引用 · ${item.required_fields.length} 个依赖字段`, `${item.evidence_ids.length} citations · ${item.required_fields.length} required fields`)}</small>
          {dataset && item.required_fields.some(field => !dataset.fields.includes(field)) && <small className="rs-warning-text">{t("数据条件未满足", "Data requirements not met")}</small>}
        </button>)}
      </aside>
      <article className="panel rs-evidence-detail">
        <div className="rs-detail-heading"><h3>{hypothesis.id}</h3><span className="rs-tag">{t("引用与解释分别判断", "Judge citations and interpretations separately")}</span></div>
        <div className="rs-evidence-compare">
          <section className="rs-source-column"><h4>{t("论文证据", "Source evidence")}</h4><p className="rs-caption">{paper?.title || research.paper_id}</p>
            {hypothesis.evidence_ids.map(id => {
              const evidence = task.evidence.find(item => item.id === id);
              if (!evidence) return <p className="rs-notice warning" key={id}>{t("缺少证据记录：", "Missing evidence record: ")}{id}</p>;
              const page = paper?.pages?.find(item => item.page === evidence.page);
              const matches = page ? quoteMatches(evidence.quote, page.text) : undefined;
              return <div className="rs-source-record" key={id}><a href={`${pdfUrl}#page=${evidence.page}`} target="_blank" rel="noreferrer">{t(`打开原文第 ${evidence.page} 页`, `Open source page ${evidence.page}`)}</a><blockquote>{evidence.quote}</blockquote>
                <p className={`rs-source-status ${matches === false ? "rs-warning-text" : ""}`}>{matches === true ? t("已在本页提取文本中匹配", "Matched in this page’s extracted text") : matches === false ? t("未在本页提取文本中匹配，请核对 PDF", "Not matched in this page’s extracted text; inspect the PDF") : t("页级文本尚未载入，引用匹配待核对", "Page text is not loaded; citation matching is unverified")}</p></div>;
            })}
            {!hypothesis.evidence_ids.length && <p className="rs-notice warning">{t("此假说尚未关联原文证据。", "This hypothesis has no linked source evidence.")}</p>}
            <p className="rs-caption">{t("字面引用匹配不代表经济机制或论文忠实度已通过审核。", "A literal text match does not establish approval of the economic mechanism or paper fidelity.")}</p>
          </section>
          <section className="rs-interpretation-column"><h4>{t("假说与解释", "Hypothesis and interpretation")}</h4>
            <span className="rs-tag">{t("主张归属：", "Claim attribution: ")}{originName(hypothesis.attribution, t)}</span><p className="rs-original-text">{hypothesis.claim}</p>
            <div className="rs-mechanism"><h4>{t("经济机制", "Economic mechanism")}</h4><span className="rs-tag warning">{originName(hypothesis.mechanism_attribution, t)}</span><p className="rs-original-text">{hypothesis.economic_mechanism ?? t("未提供 / 未决；仅公式工程验证，研究有效性仍待评定。", "Not provided / unresolved; formula engineering verification only, with research validity unassessed.")}</p></div>
            <h4>{t("信号方向", "Signal direction")}</h4><p className="rs-original-text">{hypothesis.signal_direction}</p>
          </section>
        </div>
        <div className="rs-requirements"><section><h4>{t("数据需求", "Data requirements")}</h4><RequiredFields fields={hypothesis.required_fields} dataset={dataset} />{!dataset && <p className="rs-caption">{t("数据字段尚未载入，字段可用性待核对。", "Dataset fields are not loaded; availability is unverified.")}</p>}</section><section><h4>{t("关联候选", "Linked candidates")}</h4><div className="rs-linked-candidates">{task.candidates.filter(item => item.hypothesis_id === hypothesis.id).map(item => <button className="rs-text-button" key={item.id} onClick={() => { selectCandidate(item); onStep("candidates"); }}>{item.id}</button>)}</div>{!task.candidates.some(item => item.hypothesis_id === hypothesis.id) && <p className="rs-caption">{t("此假说尚无关联候选。", "No candidates are linked to this hypothesis.")}</p>}</section></div>
        <section className="rs-assumptions"><h4>{t("适用条件与限制（原始记录）", "Assumptions and constraints (original record)")}</h4>{hypothesis.assumptions.length ? <ul>{hypothesis.assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="rs-caption">{t("未记录适用条件。", "No assumptions recorded.")}</p>}</section>
      </article>
    </div> : <div className="panel rs-empty"><h3>{t("尚未定义假说", "No hypotheses defined")}</h3><p>{t("创建新版本，补充原文引用、主张和适用条件。", "Create a new version to add source citations, claims and assumptions.")}</p></div>}
    {next(t("浏览选择用于逐条核对，不代表研究已采纳。", "Selection helps you inspect each record; it does not indicate research adoption."), t("继续查看候选", "Inspect candidates"), "candidates")}
  </section>;
  return <section id="daily-candidates" className="research-stage" aria-label={t("候选", "Candidates")}>
    {heading(t("检查候选的可执行条件", "Check whether candidates can execute"), t(`实验将执行当前版本中的全部 ${task.candidates.length} 个候选。要调整集合或公式，请保存新版本。`, `An experiment executes all ${task.candidates.length} candidates in this version. Save a new version to change the set or formulas.`))}
    {!preflight && <p className="rs-notice">{t("所选版本没有可直接复用的当前预检。实际提交后按该版本验证，不使用其他版本的预检结论。", "The selected version has no reusable current preflight. Submission validates this version without borrowing another version’s conclusions.")}</p>}
    {task.candidates.length > 0 && <div className="panel rs-candidate-table"><div className="rs-table-scroll"><table><caption className="rs-sr-only">{t("当前研究版本的候选及静态预检状态", "Candidates and static preflight status for the selected research version")}</caption><thead><tr><th scope="col">{t("候选", "Candidate")}</th><th scope="col">{t("依赖字段", "Required fields")}</th><th scope="col">{t("预检状态", "Preflight status")}</th><th scope="col">{t("公式归属", "Formula attribution")}</th></tr></thead><tbody>{task.candidates.map(item => {
      const linked = task.hypotheses.find(row => row.id === item.hypothesis_id);
      const check = preflight?.candidates.find(row => row.candidate_id === item.id);
      return <tr key={item.id} className={candidate?.id === item.id ? "selected" : undefined}><th scope="row"><button className="rs-table-select" aria-pressed={candidate?.id === item.id} onClick={() => selectCandidate(item)}>{item.id}</button></th><td className="rs-table-fields">{linked?.required_fields.join(", ") || t("未声明", "Not declared")}</td><td><span className={`rs-tag ${check?.status === "blocked" || check?.status === "warning" ? "warning" : ""}`}>{checkLabel(check, t)}</span>{check?.missing_fields?.length ? <small className="rs-table-gap">{t("缺少 ", "Missing ")}{check.missing_fields.join(", ")}</small> : null}</td><td>{originName(item.origin, t)}</td></tr>;
    })}</tbody></table></div></div>}
    {candidate ? <article className="panel rs-candidate-detail">
      <div className="rs-detail-heading"><div><h3>{candidate.id}</h3><p className="rs-caption">{t(`版本内候选 · v${revision.number}`, `Version-bound candidate · v${revision.number}`)} · {originName(candidate.origin, t)}</p></div>{candidateHypothesis && <button className="rs-text-button" onClick={() => { selectHypothesis(candidateHypothesis); onStep("evidence"); }}>{t("回看假说与原文", "Revisit hypothesis and source")}</button>}</div>
      <div className="rs-detail-fields"><h4>{t("依赖字段", "Required fields")}</h4><RequiredFields fields={candidateHypothesis?.required_fields || []} dataset={dataset} /></div>
      <div className="rs-formula-comparison"><section><h4>{t("声明公式", "Declared formula")}</h4><pre><code>{candidate.expression}</code></pre></section><section><h4>{candidateCheck?.suggested_expression ? t("预检建议表达式", "Preflight suggested expression") : t("执行条件", "Execution requirements")}</h4>
        {candidateCheck?.suggested_expression ? <><pre><code>{candidateCheck.suggested_expression}</code></pre><p className="rs-caption">{t("此为预检建议，不是实际执行记录。执行模式与冻结结果决定实际使用的表达式。", "This is a preflight suggestion, not an execution record. The execution mode and frozen result establish which expression is used.")}</p></> : <div className={`rs-execution-condition ${candidateCheck?.status === "blocked" ? "warning" : ""}`}><strong>{checkLabel(candidateCheck, t)}</strong><p>{candidateCheck?.status === "blocked" ? t("当前数据或公式不满足执行条件；保留阻断原因，不隐式替代字段。", "The current data or formula does not meet execution requirements. Preserve the blocking reason; do not substitute fields implicitly.") : t("实际执行表达式将在实验结果中保存。预检不代表实验已完成或研究结论成立。", "The actual expression is saved with experiment results. Preflight does not establish experiment completion or a research conclusion.")}</p></div>}
      </section></div>
      <div className="rs-candidate-explanation"><section><h4>{t("关联假说", "Linked hypothesis")}</h4>{candidateHypothesis ? <><p className="rs-original-text">{candidateHypothesis.claim}</p><p className="rs-caption">{t("机制归属：", "Mechanism attribution: ")}{originName(candidateHypothesis.mechanism_attribution, t)}</p><p className="rs-original-text">{candidateHypothesis.economic_mechanism}</p></> : <p className="rs-notice warning">{t("缺少关联假说：", "Missing linked hypothesis: ")}{candidate.hypothesis_id}</p>}</section><section><h4>{t("预检依据", "Preflight basis")}</h4>{candidateCheck ? <><p><code>{candidateCheck.code}</code></p>{candidateCheck.missing_fields?.length ? <p className="rs-warning-text">{t("缺少 ", "Missing ")}{candidateCheck.missing_fields.join(", ")}{t("；不得隐式替代字段。", "; no implicit field substitution.")}</p> : null}{typeof candidateCheck.warmup_rows === "number" && <p className="rs-caption">{t(`预热需求：${candidateCheck.warmup_rows} 行`, `Warm-up requirement: ${candidateCheck.warmup_rows} rows`)}</p>}</> : <p className="rs-caption">{t("未取得本版本的静态预检依据。", "No static preflight basis is available for this version.")}</p>}<p className="rs-caption">{t("通过预检只说明静态条件，不保证论文忠实度或投资有效性。", "Passing preflight establishes static conditions only, not paper fidelity or investment merit.")}</p></section></div>
      <section className="rs-change-notes"><h4>{t("变更说明（原始记录）", "Change notes (original record)")}</h4>{candidate.changes.length ? <ul>{candidate.changes.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="rs-caption">{t("此候选未记录公式改动。", "No formula changes are recorded for this candidate.")}</p>}</section>
    </article> : <div className="panel rs-empty"><h3>{t("尚未定义候选", "No candidates defined")}</h3><p>{t("创建新版本，关联假说并填写公式后再配置实验。", "Create a new version, link a hypothesis and provide a formula before configuring an experiment.")}</p></div>}
    {preflight && <JsonDetails value={preflight} label={t("查看预检范围与逐项依据", "View preflight scope and per-candidate basis")} />}
    {next(t("当前选择只切换详情。提交实验仍使用已保存版本中的完整候选集合。", "Selection changes the detail view only. Submission still uses the full candidate set in the saved version."), t("继续配置实验", "Configure experiment"), "experiments", !task.candidates.length)}
  </section>;
}
