import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { describeError, shortId } from "../domain";
import { useI18n } from "../i18n";
import type { CandidateSeriesResponse } from "../generated/api-contract";
import { assertSeriesTarget, chartPoints, plotSeries, seriesPath, seriesTargetKey, seriesValue } from "../candidateSeries";
import type { SeriesMetric, SeriesTarget } from "../candidateSeries";
import "./resultReview.css";

function useSeriesLabels() {
  const { t } = useI18n();
  return {
    gross_return: t("每日毛收益", "Daily gross return"),
    cumulative_gross_return: t("算术累计毛收益", "Arithmetic cumulative gross return"),
    rank_ic: t("每日 Rank IC", "Daily Rank IC"),
    coverage: t("有效资产覆盖比例", "Available asset coverage"),
  };
}
function usePointExplanation() {
  const { t } = useI18n();
  return (point: CandidateSeriesResponse["points"][number]) => {
    const known: Record<string, string> = {
      label_would_cross_split_boundary: t("持有期将跨越评估边界，未计算覆盖与收益", "Holding period crosses the evaluation boundary; coverage and return are not calculated"),
      insufficient_finite_assets: t("有效因子资产不足", "Insufficient assets with finite factor values"),
      constant_factor: t("因子横截面为常量，不能形成排名组合", "Constant cross-sectional factor; a ranked portfolio cannot be formed"),
    };
    if (point.reason) return known[point.reason] || point.reason;
    return point.rank_ic_state === "constant_forward_returns"
      ? t("后续收益为常量，Rank IC 未定义；毛收益仍有效", "Forward returns are constant; Rank IC is undefined but gross return remains valid")
      : t("已评估", "Evaluated");
  };
}
function LineChart({ series, metric, label }: { series: CandidateSeriesResponse; metric: SeriesMetric; label: string }) {
  const { t } = useI18n();
  const host = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(620);
  useEffect(() => {
    if (!host.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(entries => {
      const measured = entries[0]?.contentRect.width;
      if (measured && measured > 0) setWidth(Math.max(240, measured));
    });
    observer.observe(host.current);
    return () => observer.disconnect();
  }, []);
  const bounds = metric === "coverage" ? [0, 1] as const : metric === "rank_ic" ? [-1, 1] as const : undefined;
  const plot = plotSeries(chartPoints(series, metric), bounds);
  const left = 64, right = width - 14, top = 24, bottom = 200;
  const x = (value: number) => left + (value - plot.left) / (plot.right - plot.left) * (right - left);
  const y = (value: number) => top + (value - plot.top) / (plot.bottom - plot.top) * (bottom - top);
  const percent = metric !== "rank_ic";
  const format = (value: number) => percent ? (value * 100).toFixed(2) + "%" : value.toFixed(2);
  const date = (value: number | null) => value === null ? "" : new Date(value).toISOString().slice(0, 10);
  return <figure className="result-line-chart" aria-label={label}>
    <figcaption>{label}</figcaption>
    <div ref={host}>
      {plot.segments.length ? <svg viewBox={"0 0 " + width + " 250"} role="img"
        aria-label={label + t("；精确值见逐日明细", "; exact values are in the daily details")}>
        {[plot.min, (plot.min + plot.max) / 2, plot.max].map(value => {
          const position = bottom - (value - plot.min) / (plot.max - plot.min) * (bottom - top);
          return <g key={value}><line x1={left} x2={right} y1={position} y2={position} className="result-chart-grid"/>
            <text x={left - 8} y={position + 4} textAnchor="end">{format(value)}</text></g>;
        })}
        <line x1={left} y1={y(plot.zero)} x2={right} y2={y(plot.zero)} className="result-chart-zero"/>
        <text x={left} y={232}>{date(plot.first)}</text>
        <text x={right} y={232} textAnchor="end">{date(plot.last)}</text>
        {plot.segments.map((segment, index) => <g key={index}>
          <polyline className="result-chart-line" points={segment.map(point => x(point.x) + "," + y(point.y)).join(" ")} data-segment-length={segment.length}/>
          {segment.map(point => <circle key={point.signalDate} cx={x(point.x)} cy={y(point.y)} r={segment.length === 1 ? 3 : 1.6}
            className="result-chart-dot" data-value={point.value!} data-date={point.date!}>
            <title>{point.date + ": " + seriesValue(point.value, percent) + t("；信号日 ", "; signal date ") + point.signalDate}</title>
          </circle>)}
        </g>)}
      </svg> : <p className="result-chart-empty">{t("本序列没有可绘制值；请在逐日明细中查看原因。", "There are no plottable values in this series. See the daily details for the reasons.")}</p>}
    </div>
  </figure>;
}
function DailyDetails({ series }: { series: CandidateSeriesResponse }) {
  const { t } = useI18n();
  const explain = usePointExplanation();
  const [offset, setOffset] = useState(0), pageSize = 10;
  const rows = series.points.slice(offset, offset + pageSize);
  return <details className="candidate-series-details"><summary>{t("逐日明细与缺失原因", "Daily values and missing-data reasons")} ({series.points.length} {t("个信号日", "signal dates")})</summary>
    <p className="fine-print">{t("“—”表示未计算或不适用，真实零值仍显示为 0。收益以退出日归属，IC 和覆盖以信号日归属。", "A dash means not calculated or not applicable; a true zero stays zero. Returns use exit dates; IC and coverage use signal dates.")}</p>
    <div className="table-scroll"><table aria-label={t("候选逐日计算明细", "Candidate daily calculation details")}><thead><tr>
      <th>{t("信号日", "Signal date")}</th><th>{t("入场日 → 退出日", "Entry date → Exit date")}</th><th>{t("状态 / 原因", "Status / reason")}</th>
      <th>{t("日毛收益", "Daily gross return")}</th><th>{t("算术累计", "Arithmetic cumulative")}</th><th>Rank IC</th><th>{t("有效资产 / 总资产", "Available / total assets")}</th><th>{t("覆盖", "Coverage")}</th>
    </tr></thead><tbody>{rows.map(point => <tr key={point.signal_date}>
      <th scope="row">{point.signal_date}</th><td>{point.entry_date || "—"} → {point.exit_date || "—"}</td>
      <td>{point.status === "purged" ? t("边界剔除", "Boundary-purged") : point.status === "skipped" ? t("跳过", "Skipped") : t("已评估", "Evaluated")}: {explain(point)}</td>
      <td>{seriesValue(point.gross_return)}</td><td>{seriesValue(point.cumulative_gross_return)}</td><td>{seriesValue(point.rank_ic, false)}</td>
      <td>{point.available_assets === null ? t("不适用", "Not applicable") : point.available_assets + " / " + series.data.universe_size}</td><td>{seriesValue(point.coverage)}</td>
    </tr>)}</tbody></table></div>
    <div className="button-row"><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>{t("上一页明细", "Previous details")}</button>
      <span>{series.points.length ? offset + 1 : 0}–{Math.min(offset + pageSize, series.points.length)} / {series.points.length}</span>
      <button type="button" disabled={offset + pageSize >= series.points.length} onClick={() => setOffset(offset + pageSize)}>{t("下一页明细", "Next details")}</button></div>
  </details>;
}
export function CandidateSeriesView({ series }: { series: CandidateSeriesResponse }) {
  const { t } = useI18n();
  const labels = useSeriesLabels();
  const [metric, setMetric] = useState<SeriesMetric>("cumulative_gross_return");
  const returns = metric === "gross_return" || metric === "cumulative_gross_return";
  return <div className="result-series-view" data-series-candidate={series.candidate_id} data-series-attempt={series.attempt_id} data-series-digest={series.result_digest}>
    <p className="fine-print">{t("冻结版本 ", "Frozen revision ")}{shortId(series.revision_id)} · {t("尝试 ", "Attempt ")}{shortId(series.attempt_id)} · {t("数据 ", "Data ")}{series.data.version} · {series.evaluation.start} {t("至", "to")} {series.evaluation.end}</p>
    <p className="info-note">{series.data.data_kind === "synthetic" && t("合成数据，仅用于检查软件链路。", "Synthetic data for software workflow checks only. ")}{t("收益未扣交易成本；累计为有效日毛收益之和，不是复利净值或 BRAIN PnL。缺失日断线，不补零。", "Returns exclude trading costs. Cumulative values are the sum of valid daily gross returns, not compounded NAV or BRAIN PnL. Missing days break the line and are never filled with zero.")}</p>
    {series.reason && <p role="status">{series.reason}</p>}
    <div className="result-metric-options" role="group" aria-label={t("选择图表指标", "Choose a chart metric")}>
      {(Object.keys(labels) as SeriesMetric[]).map(key => <button type="button" key={key} aria-pressed={metric === key} onClick={() => setMetric(key)}>{labels[key]}</button>)}
    </div>
    <LineChart series={series} metric={metric} label={labels[metric]}/>
    <p className="fine-print">{returns ? t("毛收益按退出日定位；算术累计直接读取工具输出，不在图表中重新累计。", "Gross returns use exit dates. Arithmetic cumulative values are read directly from tool output, not recalculated in the chart.")
      : metric === "rank_ic" ? t("Rank IC 按信号日定位，衡量信号与后续持有期收益的排名相关性；未定义时留空。", "Rank IC uses signal dates and measures rank correlation with subsequent holding-period returns. Undefined values remain missing.")
        : t("覆盖率按信号日定位，纵轴固定 0–1；边界剔除日不适用。", "Coverage uses signal dates and a fixed 0–1 axis. Boundary-purged days are not applicable.")}</p>
    <p className="result-series-counts">{t("已评估 ", "Evaluated ")}{series.summary.evaluated_days} · {t("跳过 ", "Skipped ")}{series.summary.skipped_days} · {t("边界剔除 ", "Boundary-purged ")}{series.summary.purged_days} · {t("回撤：当前接口未提供", "Drawdown: not provided by this API")}</p>
    <DailyDetails key={seriesTargetKey(series)} series={series}/>
    <details><summary>{t("图表来源与计算限制", "Chart provenance and calculation limits")}</summary>
      <p className="fine-print">{t("结果摘要：", "Result digest: ")}<code>{series.result_digest}</code><br/>{t("产物摘要：", "Artifact digest: ")}<code>{series.source.result_sha256}</code><br/>{t("来源：", "Source: ")}{series.source.artifact_name} · {series.source.json_pointer}</p>
      <ul>{series.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul>
    </details>
  </div>;
}
export default function CandidateCharts({ target }: { target: SeriesTarget }) {
  const { t } = useI18n();
  const key = seriesTargetKey(target);
  const { run_id, revision_id, attempt_id, candidate_id, result_digest } = target;
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<{ key: string; series?: CandidateSeriesResponse; error?: string }>({ key });
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setState({ key });
    const expected: SeriesTarget = { run_id, revision_id, attempt_id, candidate_id, result_digest };
    api<CandidateSeriesResponse>(seriesPath(expected), { signal: controller.signal }).then(series => {
      assertSeriesTarget(series, expected);
      if (active) setState({ key, series });
    }).catch(error => { if (active && !(error instanceof DOMException && error.name === "AbortError")) setState({ key, error: describeError(error) }); });
    return () => { active = false; controller.abort(); };
  }, [key, retry, run_id, revision_id, attempt_id, candidate_id, result_digest]);
  const current = state.key === key ? state : null;
  return <section aria-label={t("候选结果图表", "Candidate result charts")} className="candidate-series" aria-busy={!current?.series && !current?.error}>
    <h4>{t("逐日计算结果", "Daily computed results")}</h4>
    {current?.series ? <CandidateSeriesView key={key} series={current.series}/> : current?.error ? <div role="alert">
      <p>{t("图表暂不可用：", "The chart is unavailable: ")}{current.error}</p><button type="button" onClick={() => setRetry(value => value + 1)}>{t("重试读取图表", "Retry chart loading")}</button>
    </div> : <p role="status">{t("正在读取本次候选的已核验时序…", "Loading the verified series for this candidate…")}</p>}
  </section>;
}
