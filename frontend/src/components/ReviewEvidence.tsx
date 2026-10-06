import { describeError, shortId } from "../domain";
import type { Artifact, Run } from "../domain";
import { observedMetric, reviewEvidence } from "../reviewEvidence";
import { Badge, JsonDetails } from "./ui";
import CandidateCharts from "./CandidateCharts";

const origins: Record<string, string> = {paper_original:"论文原文", user_modification:"本人修改", model_conjecture:"模型推测"};
const text = (value: unknown) => typeof value === "string" ? value : "未记录";
const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

export default function ReviewEvidence({run, candidateId, paperId, artifacts}: {
  run: Run; candidateId: string; paperId: string; artifacts: Artifact[];
}) {
  const context = reviewEvidence(run, candidateId, artifacts);
  if (!context) return <section aria-label="本次候选输出" className="review-output">
    <h3>本次候选输出</h3><p role="status">当前输出尚未通过完整性校验或缺少审核目标，暂不展示指标作为审核依据。</p>
    {!!run.error && <p>{describeError(run.error)}</p>}
  </section>;
  const {candidate, target, declared, hypothesis, evidence, outputFiles, report} = context;
  const result = candidate.result;
  const metrics = result?.metrics;
  const metadata = result?.market_metadata;
  const split = result?.split;
  const period = object(object(result?.config?.splits)[split || ""]);
  const href = (id: string) => `/api/runs/${encodeURIComponent(run.id)}/artifacts/${encodeURIComponent(id)}`;
  const failures = candidate.attempts?.filter(attempt => attempt.error || attempt.reason || attempt.repair) || [];
  return <section aria-label="本次候选输出" className="review-output">
    <h3>本次候选输出 · {candidate.id} <Badge status={candidate.status}/></h3>
    <p className="fine-print">实验 {shortId(run.id)} · 尝试 {shortId(target.attempt_id)} · 结果完整性在本次加载时已核验。下方内容来自该实验的冻结输入与计算输出。</p>
    <h4>声明公式</h4><code className="expression">{declared?.expression || "冻结任务未提供声明公式"}</code>
    <h4>实际执行公式</h4><code className="expression">{result?.expression || "本次未产生计算结果；参见执行记录"}</code>
    {declared && <p className="fine-print">来源：{origins[declared.origin] || declared.origin}{declared.changes.length > 0 && `；修改：${declared.changes.join("；")}`}</p>}
    <h4>论文证据与研究假设</h4>
    {evidence.map(item => <div key={item.id} className="review-quote">
      <a href={`/api/papers/${encodeURIComponent(paperId)}/pdf#page=${item.page}`} target="_blank" rel="noreferrer">查看论文第 {item.page} 页</a>
      <blockquote>{item.quote}</blockquote>
    </div>)}
    {!evidence.length && <p>冻结任务中没有找到关联引文，请保留为未评定。</p>}
    {hypothesis && <>
      <p><strong>研究假设（{origins[hypothesis.attribution] || hypothesis.attribution}）：</strong>{hypothesis.claim}</p>
      <p><strong>经济解释（{origins[hypothesis.mechanism_attribution] || hypothesis.mechanism_attribution}）：</strong>{hypothesis.economic_mechanism}</p>
      <p><strong>信号方向：</strong>{hypothesis.signal_direction}</p>
      <p><strong>所需字段：</strong>{hypothesis.required_fields.join("、")}</p>
      <details><summary>适用条件与字段语义</summary><ul>{hypothesis.assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul>
        <JsonDetails value={metadata?.field_availability || null} label="查看字段可用时间"/>
      </details>
    </>}
    <h4>计算工具输出</h4>
    {(candidate.reason || result?.reason) && <p className="failure-reason">{candidate.reason || result?.reason}</p>}
    <p>{metadata?.data_kind === "synthetic" ? "合成数据 · 用于软件验证" : `数据类型：${text(metadata?.data_kind)}`} · {text(metadata?.version)}<br/>
      {split === "validation" ? "验证区间" : `评估区间：${split || "未记录"}`}：{text(period.start)} 至 {text(period.end)}</p>
    {metrics ? <div className="table-scroll"><table aria-label="候选计算指标"><thead><tr><th>指标</th><th>实际值</th><th>如何阅读</th></tr></thead><tbody>
      <tr><td>平均 Rank IC</td><td>{observedMetric(metrics.mean_rank_ic)}</td><td>信号排名与后续收益排名的相关性；正负号应结合声明方向理解。</td></tr>
      <tr><td>平均日毛收益</td><td>{observedMetric(metrics.mean_gross_return, true)}</td><td>有效日的组合毛收益均值，未扣交易成本。</td></tr>
      <tr><td>有效评估天数</td><td>{typeof metrics.evaluated_days === "number" ? metrics.evaluated_days : "未计算"}</td><td>检查样本是否足够，以及有多少天被跳过。</td></tr>
      <tr><td>因子覆盖率</td><td>{observedMetric(metrics.factor_coverage, true)}</td><td>有限因子值占可评估观测的比例；覆盖率高不等于因子有效。</td></tr>
      <tr><td>跳过 / 边界剔除天数</td><td>{metrics.skipped_days ?? "未计算"} / {metrics.purged_days ?? "未计算"}</td><td>缺失或常量信号，以及跨时间区间的收益标签，分别核对。</td></tr>
    </tbody></table></div> : <p className="failure-reason">{candidate.reason || result?.reason || describeError(candidate.error) || describeError(run.state?.error) || describeError(run.error) || "本次没有可评估指标，请查看执行记录。"}</p>}
    {metrics && <p className="fine-print">覆盖观测：{metrics.finite_factor_observations ?? "未计算"} / {metrics.possible_factor_observations ?? "未计算"}；Rank IC 有效日：{metrics.rank_ic_days ?? "未计算"}。</p>}
    {metadata?.data_kind === "synthetic" && <p className="info-note">这些数值用于检查实现和评估链路。正收益或高 IC 不能证明真实投资价值，负值也不自动意味着实现错误。</p>}
    {result && <CandidateCharts key={run.client_loaded_at} target={{run_id:run.id,revision_id:run.revision_id,...target}}/>}
    <div className="button-row">
      {outputFiles.filter(file => file.name.endsWith("/factor.csv") || file.name.endsWith("/result.json")).map(file => <a className="button small" key={file.id} href={href(file.id)} download>{file.name.endsWith("/factor.csv") ? "下载因子 CSV" : "下载计算结果 JSON"}</a>)}
      {report && <a className="button small" href={href(report.id)} download>下载本次实验报告</a>}
    </div>
    {!outputFiles.length && <p className="fine-print">此候选暂无已关联的计算文件；指标缺失时请核对执行记录。</p>}
    <details open={!metrics || undefined}><summary>执行记录、完整指标与计算约定</summary>
      <JsonDetails value={candidate.attempts || []} label="查看执行尝试与错误"/>
      {failures.length > 0 && <p>保留了 {failures.length} 条包含错误、原因或修正信息的尝试。</p>}
      <JsonDetails value={metrics || null} label="查看完整指标"/>
      <JsonDetails value={result?.execution || null} label="查看信号与成交时序"/>
      <JsonDetails value={result?.factor_diagnostics || null} label="查看缺失值与算子诊断"/>
      <JsonDetails value={result?.limitations || []} label="查看评估限制"/>
      <JsonDetails value={{run_id:run.id, revision_id:run.revision_id, ...target}} label="查看完整审核目标"/>
    </details>
    <p className="fine-print">审核依据可以写：原文与实现是否一致、经济解释是否标明推测、数据和计算条件是否符合预期。证据不足时保留未评定或记录需要修改；完整性校验只证明文件一致。</p>
  </section>;
}
