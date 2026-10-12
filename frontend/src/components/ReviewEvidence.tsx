import { describeError, shortId } from "../domain";
import type { Artifact, Run } from "../domain";
import { observedMetric, reviewEvidence } from "../reviewEvidence";
import { useI18n } from "../i18n";
import { Badge, JsonDetails } from "./ui";
import CandidateCharts from "./CandidateCharts";
import EvidenceLineage from "./EvidenceLineage";
import FormulaAlignment from "./FormulaAlignment";
import FormulaPresentation from "./FormulaPresentation";
import "./resultReview.css";

const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
export default function ReviewEvidence({ run, candidateId, artifacts }: {
  run: Run; candidateId: string; artifacts: Artifact[];
}) {
  const { t } = useI18n();
  const origins: Record<string, string> = { paper_original: t("论文原文", "Paper original"), user_modification: t("用户/项目修改", "User/project modification"), model_conjecture: t("模型推测", "Model conjecture") };
  const text = (value: unknown) => typeof value === "string" ? value : t("未记录", "Not recorded");
  const metric = (value: unknown, percent = false) => typeof value === "number" && Number.isFinite(value) ? observedMetric(value, percent) : t("未计算", "Not calculated");
  const context = reviewEvidence(run, candidateId, artifacts);
  if (!context) return <section aria-label={t("本次候选输出", "Current candidate output")} className="review-output">
    <h3>{t("本次候选输出", "Current candidate output")}</h3>
    <p role="status">{t("当前输出尚未通过完整性校验或缺少审核目标，暂不展示指标作为审核依据。", "This output has not passed integrity verification or has no review target. Metrics are not shown as review evidence.")}</p>
    {!!run.error && <p>{describeError(run.error)}</p>}
  </section>;
  const { candidate, target, declared, hypothesis, evidence, evidenceReferences, sourceIssues, outputFiles, report,
    frozenPaper, taskFile, candidateFile, candidateExportHref } = context;
  const issueNames: Record<string, string> = {
    PAPER_IDENTITY_CONFLICT: t("关联引文的冻结论文 ID 不一致", "Linked quotations have conflicting frozen paper IDs"),
    DECLARATION_AMBIGUOUS: t("声明候选 ID 重复", "Duplicate declared candidate ID"), DECLARATION_MISSING: t("缺少冻结候选声明", "Frozen candidate declaration missing"),
    HYPOTHESIS_MISMATCH: t("声明与执行的假说 ID 不一致", "Declared and executed hypothesis IDs differ"), HYPOTHESIS_AMBIGUOUS: t("假说 ID 重复", "Duplicate hypothesis ID"), HYPOTHESIS_MISSING: t("缺少关联假说", "Linked hypothesis missing"),
    EVIDENCE_REFERENCES_MISSING: t("假说未记录证据 ID", "Hypothesis has no evidence IDs"), EVIDENCE_ID_INVALID: t("证据 ID 无效", "Invalid evidence ID"),
    EVIDENCE_REFERENCE_DUPLICATE: t("证据引用 ID 重复", "Duplicate evidence reference ID"), EVIDENCE_AMBIGUOUS: t("证据 ID 对应多条引文", "Evidence ID matches multiple quotations"), EVIDENCE_MISSING: t("缺少关联引文", "Linked quotation missing"), EVIDENCE_INVALID: t("引文或物理页码无效", "Invalid quotation or physical page"),
    CITATION_PROOF_AMBIGUOUS: t("冻结字面核对记录重复", "Duplicate frozen literal-match proof"), CITATION_PROOF_UNAVAILABLE: t("缺少一致的冻结字面核对记录", "Consistent frozen literal-match proof unavailable"),
    FROZEN_SOURCE_TARGET_MISMATCH: t("冻结来源证明与本次实验、版本、尝试或状态目标不一致", "Frozen source proof does not match this run, revision, attempt or state target"),
    PDF_TARGET_UNAVAILABLE: t("缺少当前尝试的完整状态目标", "Current attempt state target unavailable"), PDF_ARTIFACT_AMBIGUOUS: t("冻结 PDF 文件身份不唯一", "Frozen PDF identity is ambiguous"), PDF_ARTIFACT_UNAVAILABLE: t("冻结 PDF 缺失或摘要不匹配", "Frozen PDF missing or digest mismatch"),
  };
  const evidenceAnchor = (id: string) => "review-evidence-" + encodeURIComponent(candidate.id) + "-" + encodeURIComponent(id);
  const documents = [...new Map(evidenceReferences.filter(reference => reference.source).map(reference => [reference.source!.documentSha256, reference.source!])).values()];
  const result = candidate.result, metrics = result?.metrics, metadata = result?.market_metadata, split = result?.split;
  const period = object(object(result?.config?.splits)[split || ""]);
  const href = (id: string) => "/api/runs/" + encodeURIComponent(run.id) + "/artifacts/" + encodeURIComponent(id);
  const failures = candidate.attempts?.filter(attempt => attempt.error || attempt.reason || attempt.repair) || [];
  return <section aria-label={t("本次候选输出", "Current candidate output")} className="review-output"
    data-review-run={run.id} data-review-revision={run.revision_id}
    data-review-candidate={candidate.id} data-review-attempt={target.attempt_id} data-review-digest={target.result_digest}>
    <div className="result-section-heading"><h3>{t("本次候选输出", "Current candidate output")} · {candidate.id}</h3><Badge status={candidate.status}/></div>
    <p className="fine-print">{t("实验 ", "Run ")}{shortId(run.id)} · {t("冻结版本 ", "Frozen revision ")}{shortId(run.revision_id)} · {t("尝试 ", "Attempt ")}{shortId(target.attempt_id)} · {t("结果完整性在本次加载时已核验。下方内容来自该实验的冻结输入与计算输出。", "Result integrity was verified on this load. The following evidence comes from this run's frozen inputs and computed outputs.")}</p>
    <div className="review-output-pane">
      <h4>{t("计算工具输出", "Computed output")}</h4>
      {(candidate.reason || result?.reason) && <p className="failure-reason">{candidate.reason || result?.reason}</p>}
      <p className="result-data-scope">{metadata?.data_kind === "synthetic" ? t("合成数据 · 用于软件验证", "Synthetic data · software validation") : t("数据类型：", "Data type: ") + text(metadata?.data_kind)} · {text(metadata?.version)}<br/>
        {split === "validation" ? t("验证区间", "Validation period") : t("评估区间：", "Evaluation split: ") + (split || t("未记录", "Not recorded"))}: {text(period.start)} {t("至", "to")} {text(period.end)}</p>
      {metrics ? <div className="table-scroll"><table className="result-metrics-table" aria-label={t("候选计算指标", "Candidate computed metrics")}><thead><tr><th>{t("指标", "Metric")}</th><th>{t("实际值", "Observed value")}</th><th>{t("如何阅读", "How to interpret")}</th></tr></thead><tbody>
        <tr><th scope="row">{t("平均 Rank IC", "Mean Rank IC")}</th><td>{metric(metrics.mean_rank_ic)}</td><td>{t("信号排名与后续收益排名的相关性；正负号应结合声明方向理解。", "Rank correlation of the signal and subsequent returns; interpret its sign against the declared direction.")}</td></tr>
        <tr><th scope="row">{t("平均日毛收益", "Mean daily gross return")}</th><td>{metric(metrics.mean_gross_return, true)}</td><td>{t("有效日的组合毛收益均值，未扣交易成本。", "Mean portfolio gross return over valid days, before trading costs.")}</td></tr>
        <tr><th scope="row">{t("有效评估天数", "Evaluated days")}</th><td>{metrics.evaluated_days ?? t("未计算", "Not calculated")}</td><td>{t("检查样本是否足够，以及有多少天被跳过。", "Check sample size and the number of skipped days.")}</td></tr>
        <tr><th scope="row">{t("因子覆盖率", "Factor coverage")}</th><td>{metric(metrics.factor_coverage, true)}</td><td>{t("有限因子值占可评估观测的比例；覆盖率高不等于因子有效。", "Finite factor values as a share of evaluable observations; high coverage does not establish validity.")}</td></tr>
        <tr><th scope="row">{t("跳过 / 边界剔除天数", "Skipped / boundary-purged days")}</th><td>{metrics.skipped_days ?? t("未计算", "Not calculated")} / {metrics.purged_days ?? t("未计算", "Not calculated")}</td><td>{t("分别核对缺失或常量信号，以及跨时间区间的收益标签。", "Inspect missing or constant signals separately from return labels crossing a time boundary.")}</td></tr>
      </tbody></table></div> : <p className="failure-reason">{candidate.reason || result?.reason || describeError(candidate.error) || describeError(run.state?.error) || describeError(run.error) || t("本次没有可评估指标，请查看执行记录。", "This attempt has no evaluable metrics. Inspect its execution record.")}</p>}
      {metrics && <p className="fine-print">{t("覆盖观测：", "Covered observations: ")}{metrics.finite_factor_observations ?? t("未计算", "Not calculated")} / {metrics.possible_factor_observations ?? t("未计算", "Not calculated")} · {t("Rank IC 有效日：", "Days with defined Rank IC: ")}{metrics.rank_ic_days ?? t("未计算", "Not calculated")}</p>}
      {metadata?.data_kind === "synthetic" && <p className="info-note">{t("这些数值用于检查实现和评估链路。正收益或高 IC 不能证明真实投资价值，负值也不自动意味着实现错误。", "These values check implementation and evaluation behavior. Positive returns or high IC do not prove investment value; negative values do not automatically indicate an implementation error.")}</p>}
      {result && <CandidateCharts key={run.client_loaded_at} target={{ run_id: run.id, revision_id: run.revision_id, ...target }}/>}
    </div>
    <section id={"review-source-" + encodeURIComponent(candidate.id)} className="review-source-pane" aria-label={t("冻结依据与实现", "Frozen evidence and implementation")}>
      <h4>{t("冻结依据与实现", "Frozen evidence and implementation")}</h4>
      <p className="review-source-chain" aria-label={t("候选来源链", "Candidate source chain")}>
        <strong>{candidate.id}</strong> → {t("假说 ", "Hypothesis ")}{hypothesis?.id || t("关联未知", "Link unknown")} → {t("证据 ", "Evidence ")}
        {evidenceReferences.length ? evidenceReferences.map((reference, index) => <span key={reference.id}>{index > 0 && " · "}{reference.evidence ? <a href={"#" + evidenceAnchor(reference.id)}>{reference.id} ({t("物理页 ", "physical page ")}{reference.evidence.page})</a> : <span>{reference.id} ({t("未知", "unknown")})</span>}</span>) : t("未记录", "Not recorded")}
      </p>
      {sourceIssues.length > 0 && <div className="review-source-unknown" role="status"><p>{t("来源链存在未确认部分；计算指标可独立核对，请勿将这些来源关联视为已通过。", "Parts of the source chain are unconfirmed. Computed metrics can be checked independently; these source links are not approved.")}</p><ul>{sourceIssues.map((issue, index) => <li key={issue.code + index}>{issueNames[issue.code] || issue.code}{issue.id ? ": " + issue.id : ""}</li>)}</ul></div>}
      {documents.map(source => <p key={source.documentSha256} className="fine-print review-document-identity">{t("冻结论文：", "Frozen paper: ")}{source.paperId}<br/>PDF SHA256: <code>{source.documentSha256}</code></p>)}
      <p className="fine-print">{t("下列原文、修改和推测标签均为冻结声明。字面引文匹配和文件一致不等于研究语义通过；数据来源见上方数据版本，模型身份未在此结果中记录时保持未知。", "Original, modification and conjecture labels below are frozen declarations. Literal quotation matching and file integrity do not establish semantic validity. Data provenance is shown above; model identity remains unknown when not recorded in this result.")}</p>
      <EvidenceLineage run={run} candidateId={candidate.id}/>
      <div className="result-formula-comparison">
        <div><h5>{t("声明公式", "Declared expression")}</h5><code className="expression">{declared?.expression || t("冻结任务未提供声明公式", "The frozen task has no declared expression")}</code></div>
        <div><h5>{t("实际执行公式", "Executed expression")}</h5><code className="expression">{result?.expression || t("本次未产生计算结果；参见执行记录", "No computed result was produced; see the execution record")}</code></div>
      </div>
      <FormulaPresentation run={run} candidateId={candidate.id}/>
      <FormulaAlignment run={run} candidateId={candidate.id}/>
      {declared && <p className="fine-print">{t("声明来源：", "Declared origin: ")}{origins[declared.origin] || text(declared.origin) + t("（未识别声明）", " (unrecognized declaration)")}{Array.isArray(declared.changes) && declared.changes.length > 0 && t("；修改：", "; changes: ") + declared.changes.join(t("；", "; "))}</p>}
      <div className="result-evidence-comparison">
        <div><h5>{t("论文证据", "Paper evidence")}</h5>
          {evidenceReferences.filter(reference => reference.evidence).map(reference => {
            const item = reference.evidence!;
            return <div key={item.id} id={evidenceAnchor(item.id)} className="review-quote" data-evidence-id={item.id}>
              <p className="review-evidence-label"><strong>{t("证据 ", "Evidence ")}{item.id}</strong> · {t("PDF 物理页 ", "PDF physical page ")}{item.page}</p>
              {reference.source ? <><a href={reference.source.href} target="_blank" rel="noreferrer">{t("查看冻结论文第 ", "Open frozen paper physical page ")}{item.page}{t(" 物理页", "")}</a><p className="fine-print">{t("冻结记录确认引文字面匹配；仍需核对公式、上下文和归因。页号从 PDF 第一页起计，不代表纸面编号，也不定位 AI 原始 chunk。", "The frozen record confirms a literal quotation match; review the formula, context and attribution. Pages count from the first PDF page, not printed numbering, and do not locate an original AI chunk.")}</p></> : <p className="fine-print">{t("冻结 PDF 定位不可用；保留引文声明，不借用当前论文链接。", "Frozen PDF navigation is unavailable. The declared quotation is retained without borrowing the current paper link.")}</p>}
              {item.quote.length > 900 ? <details className="review-long-quote"><summary>{t("展开完整引文", "Expand full quotation")} · {item.quote.slice(0, 180)}…</summary><blockquote>{item.quote}</blockquote></details> : <blockquote>{item.quote}</blockquote>}
            </div>;
          })}
          {!evidence.length && <p>{t("冻结任务中没有找到关联引文，请保留为未评定。", "No linked quotation was found in the frozen task. Leave this unassessed.")}</p>}
        </div>
        <div><h5>{t("研究假设", "Research hypothesis")}</h5>{hypothesis && <>
          <p><strong>{t("研究假设（", "Hypothesis (")}{origins[hypothesis.attribution] || text(hypothesis.attribution) + t("（未识别声明）", " (unrecognized declaration)")}{t("）：", "): ")}</strong>{hypothesis.claim}</p>
          <p><strong>{t("经济解释（", "Economic explanation (")}{origins[hypothesis.mechanism_attribution] || text(hypothesis.mechanism_attribution) + t("（未识别声明）", " (unrecognized declaration)")}{t("）：", "): ")}</strong>{hypothesis.economic_mechanism}</p>
          <p><strong>{t("信号方向：", "Signal direction: ")}</strong>{hypothesis.signal_direction}</p>
          <p><strong>{t("所需字段：", "Required fields: ")}</strong>{(Array.isArray(hypothesis.required_fields) ? hypothesis.required_fields : []).join(t("、", ", "))}</p>
          <details><summary>{t("适用条件与字段语义", "Conditions and field semantics")}</summary><ul>{(Array.isArray(hypothesis.assumptions) ? hypothesis.assumptions : []).map((item, index) => <li key={index}>{item}</li>)}</ul>
            <JsonDetails value={metadata?.field_availability || null} label={t("查看字段可用时间", "View field availability timing")}/>
          </details>
        </>}</div>
      </div>
    </section>
    <div className="button-row result-downloads">
      {frozenPaper && <a className="button small" href={href(frozenPaper.id)} download>{t("下载本次冻结原 PDF", "Download this run's frozen source PDF")}</a>}
      {taskFile && <a className="button small" href={href(taskFile.id)} download>{t("下载冻结任务 JSON", "Download frozen task JSON")}</a>}
      {candidateFile && <a className="button small" href={href(candidateFile.id)} download>{t("下载冻结候选 JSON", "Download frozen candidate JSON")}</a>}
      {candidateExportHref && <a className="button small" href={candidateExportHref} download={`candidate-${candidate.id}.json`}>{t("导出冻结候选声明", "Export frozen candidate declaration")}</a>}
      {outputFiles.map(file => <a className="button small" key={file.id} href={href(file.id)} download>{file.name.split("/").at(-1) === "factor.csv" ? t("下载因子 CSV", "Download factor CSV") : t("下载计算结果 JSON", "Download computed result JSON")}</a>)}
      {report && <a className="button small" href={href(report.id)} download>{t("下载本次实验报告", "Download this run report")}</a>}
    </div>
    {candidateExportHref && <p className="fine-print">{t("候选声明由本次冻结任务导出；计算结果见独立结果文件。", "The candidate declaration is exported from this run's frozen task. Computed output is in the separate result file.")}</p>}
    {!frozenPaper && <p className="fine-print">{t("本次冻结原 PDF 下载不可用，请核对冻结目标与实验文件。", "This run's frozen source PDF download is unavailable. Check the frozen target and run files.")}</p>}
    {!outputFiles.length && <p className="fine-print">{t("此候选暂无已关联的计算文件；指标缺失时请核对执行记录。", "No computation files are linked to this candidate. Check the execution record when metrics are missing.")}</p>}
    <details open={!metrics || undefined}><summary>{t("执行记录、完整指标与计算约定", "Execution records, full metrics and calculation rules")}</summary>
      <JsonDetails value={candidate.attempts || []} label={t("查看执行尝试与错误", "View attempts and errors")}/>
      {failures.length > 0 && <p>{t("保留了 ", "Retained ")}{failures.length}{t(" 条包含错误、原因或修正信息的尝试。", " attempts with errors, reasons or repairs.")}</p>}
      <JsonDetails value={metrics || null} label={t("查看完整指标", "View all metrics")}/>
      <JsonDetails value={result?.execution || null} label={t("查看信号与成交时序", "View signal and execution timing")}/>
      <JsonDetails value={result?.factor_diagnostics || null} label={t("查看缺失值与算子诊断", "View missing-data and operator diagnostics")}/>
      <JsonDetails value={result?.limitations || []} label={t("查看评估限制", "View evaluation limitations")}/>
      <JsonDetails value={{ run_id: run.id, revision_id: run.revision_id, ...target }} label={t("查看完整审核目标", "View the full review target")}/>
    </details>
    <p className="fine-print">{t("审核可检查原文与实现是否一致、经济解释是否标明推测、数据和计算条件是否符合预期。证据不足时保留未评定或记录需要修改；完整性校验只证明文件一致。", "Compare source and implementation, check attribution of conjectures, and inspect data and calculation assumptions. Leave unsupported judgments unassessed or request changes. Integrity verification establishes file consistency only.")}</p>
  </section>;
}
