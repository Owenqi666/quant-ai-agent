import { useEffect, useState } from "react";
import { api } from "../api";
import { describeError, shortId } from "../domain";
import type { CandidateSeriesResponse } from "../generated/api-contract";
import { assertSeriesTarget, chartPoints, plotSeries, pointExplanation, seriesPath, seriesTargetKey, seriesValue } from "../candidateSeries";
import type { SeriesMetric, SeriesTarget } from "../candidateSeries";
import "./candidate-charts.css";

function LineChart({series, metric, label, percent=true, bounds}: {
  series:CandidateSeriesResponse; metric:SeriesMetric; label:string; percent?:boolean; bounds?:readonly [number,number];
}) {
  const points=chartPoints(series,metric), plot=plotSeries(points,bounds);
  const format=(value:number)=>percent?`${(value*100).toFixed(2)}%`:value.toFixed(2);
  const date=(value:number|null)=>value===null?"":new Date(value).toISOString().slice(0,10);
  return <figure className="candidate-chart" aria-label={label}>
    <figcaption>{label}</figcaption>
    {plot.segments.length ? <svg viewBox="0 0 620 180" role="img" aria-label={`${label}；精确值见逐日明细`}>
      <line x1={plot.left} y1={plot.zero} x2={plot.right} y2={plot.zero} className="chart-zero"/>
      <line x1={plot.left} y1={plot.top} x2={plot.left} y2={plot.bottom} className="chart-axis"/>
      <text x={plot.left-6} y={plot.top+4} textAnchor="end">{format(plot.max)}</text>
      <text x={plot.left-6} y={plot.bottom+4} textAnchor="end">{format(plot.min)}</text>
      <text x={plot.left} y={165}>{date(plot.first)}</text>
      <text x={plot.right} y={165} textAnchor="end">{date(plot.last)}</text>
      {plot.segments.map((segment,index)=><g key={index}>
        <polyline className={`chart-line chart-${metric}`} points={segment.map(point=>`${point.x},${point.y}`).join(" ")} data-segment-length={segment.length}/>
        {segment.map(point=><circle key={point.signalDate} cx={point.x} cy={point.y} r={segment.length===1?3:1.6}
          className={`chart-dot chart-${metric}`} data-value={point.value!} data-date={point.date!}>
          <title>{point.date}：{seriesValue(point.value,percent)}；信号日 {point.signalDate}</title>
        </circle>)}
      </g>)}
    </svg> : <p className="chart-empty">本序列没有可绘制值；请在逐日明细中查看原因。</p>}
  </figure>;
}

function DailyDetails({series}: {series:CandidateSeriesResponse}) {
  const [offset,setOffset]=useState(0), pageSize=15;
  const rows=series.points.slice(offset,offset+pageSize);
  return <details className="candidate-series-details"><summary>逐日明细与缺失原因（{series.points.length} 个信号日）</summary>
    <p className="fine-print">“—”表示未计算或不适用，真实零值仍显示为 0。收益以退出日归属，IC 和覆盖以信号日归属。</p>
    <div className="table-scroll"><table aria-label="候选逐日计算明细"><thead><tr>
      <th>信号日</th><th>入场日 → 退出日</th><th>状态 / 原因</th><th>日毛收益</th><th>算术累计</th><th>Rank IC</th><th>有效资产 / 总资产</th><th>覆盖</th>
    </tr></thead><tbody>{rows.map(point=><tr key={point.signal_date}>
      <th scope="row">{point.signal_date}</th><td>{point.entry_date || "—"} → {point.exit_date || "—"}</td>
      <td>{point.status==="purged"?"边界剔除":point.status==="skipped"?"跳过":"已评估"}：{pointExplanation(point)}</td>
      <td>{seriesValue(point.gross_return)}</td><td>{seriesValue(point.cumulative_gross_return)}</td><td>{seriesValue(point.rank_ic,false)}</td>
      <td>{point.available_assets === null ? "不适用" : `${point.available_assets} / ${series.data.universe_size}`}</td><td>{seriesValue(point.coverage)}</td>
    </tr>)}</tbody></table></div>
    <div className="button-row"><button type="button" disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-pageSize))}>上一页明细</button>
      <span>{series.points.length?offset+1:0}–{Math.min(offset+pageSize,series.points.length)} / {series.points.length}</span>
      <button type="button" disabled={offset+pageSize>=series.points.length} onClick={()=>setOffset(offset+pageSize)}>下一页明细</button></div>
  </details>;
}

export function CandidateSeriesView({series}: {series:CandidateSeriesResponse}) {
  return <div data-series-candidate={series.candidate_id} data-series-attempt={series.attempt_id} data-series-digest={series.result_digest}>
    <p className="fine-print">冻结版本 {shortId(series.revision_id)} · 尝试 {shortId(series.attempt_id)} · 数据 {series.data.version} · {series.evaluation.start} 至 {series.evaluation.end}</p>
    <p className="info-note">{series.data.data_kind==="synthetic"?"合成数据，仅用于检查软件链路。":""}收益未扣交易成本；累计为有效日毛收益之和，不是复利净值或 BRAIN PnL。缺失日断线，不补零。</p>
    {series.reason && <p role="status">{series.reason}</p>}
    <section className="candidate-chart-group" aria-label="毛收益变化"><h5>毛收益变化 · 按退出日</h5>
      <div className="candidate-chart-grid"><LineChart series={series} metric="gross_return" label="每日毛收益"/>
        <LineChart series={series} metric="cumulative_gross_return" label="有效日毛收益的算术累计"/></div>
    </section>
    <div className="candidate-chart-grid">
      <section className="candidate-chart-group" aria-label="Rank IC 变化"><h5>Rank IC · 按信号日</h5>
        <p className="fine-print">衡量该信号与后续入场至退出持有期收益的排名相关性，未定义时留空。</p>
        <LineChart series={series} metric="rank_ic" label="每日 Rank IC" percent={false} bounds={[-1,1]}/></section>
      <section className="candidate-chart-group" aria-label="覆盖与跳过状态"><h5>有效资产覆盖 · 按信号日</h5>
        <p className="fine-print">已评估 {series.summary.evaluated_days} 日 · 跳过 {series.summary.skipped_days} 日 · 边界剔除 {series.summary.purged_days} 日；剔除日覆盖不适用。</p>
        <LineChart series={series} metric="coverage" label="有效资产覆盖比例" bounds={[0,1]}/>
        <div className="series-status-strip" role="img" aria-label="逐日状态；具体日期和原因见逐日明细">{series.points.map(point=><span key={point.signal_date} className={`series-status-${point.status}`} title={`${point.signal_date}：${pointExplanation(point)}`}/>)}</div>
        <p className="fine-print">状态条：深色为已评估，橙色为跳过，灰色为边界剔除。</p>
      </section>
    </div>
    <DailyDetails key={seriesTargetKey(series)} series={series}/>
    <details><summary>图表来源与计算限制</summary>
      <p className="fine-print">结果摘要：<code>{series.result_digest}</code><br/>产物摘要：<code>{series.source.result_sha256}</code><br/>来源：{series.source.artifact_name} · {series.source.json_pointer}</p>
      <ul>{series.limitations.map((limitation,index)=><li key={index}>{limitation}</li>)}</ul>
    </details>
  </div>;
}

export default function CandidateCharts({target}: {target:SeriesTarget}) {
  const key=seriesTargetKey(target);
  const {run_id,revision_id,attempt_id,candidate_id,result_digest}=target;
  const [retry,setRetry]=useState(0);
  const [state,setState]=useState<{key:string;series?:CandidateSeriesResponse;error?:string}>({key});
  useEffect(()=>{
    const controller=new AbortController();
    let active=true;
    setState({key});
    const expected:SeriesTarget={run_id,revision_id,attempt_id,candidate_id,result_digest};
    api<CandidateSeriesResponse>(seriesPath(expected),{signal:controller.signal}).then(series=>{
      assertSeriesTarget(series,expected);
      if(active)setState({key,series});
    }).catch(error=>{if(active && !(error instanceof DOMException && error.name==="AbortError"))setState({key,error:describeError(error)});});
    return ()=>{active=false;controller.abort();};
  },[key,retry,run_id,revision_id,attempt_id,candidate_id,result_digest]);
  const current=state.key===key?state:null;
  return <section aria-label="候选结果图表" className="candidate-series" aria-busy={!current?.series&&!current?.error}>
    <h4>逐日计算结果</h4>
    {current?.series ? <CandidateSeriesView series={current.series}/> : current?.error ? <div role="alert">
      <p>图表暂不可用：{current.error}</p><button type="button" onClick={()=>setRetry(value=>value+1)}>重试读取图表</button>
    </div> : <p role="status">正在读取本次候选的已核验时序…</p>}
  </section>;
}
