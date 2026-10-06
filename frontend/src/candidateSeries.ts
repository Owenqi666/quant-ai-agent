import type { CandidateSeriesResponse } from "./generated/api-contract";

export type SeriesTarget = Pick<CandidateSeriesResponse,
  "run_id" | "revision_id" | "attempt_id" | "candidate_id" | "result_digest">;
export type SeriesMetric = "gross_return" | "cumulative_gross_return" | "rank_ic" | "coverage";
export type ChartPoint = { date: string | null; value: number | null; signalDate: string };
export type PlotPoint = ChartPoint & { x: number; y: number };

export function seriesTargetKey(target: SeriesTarget): string {
  return JSON.stringify([target.run_id, target.revision_id, target.attempt_id,
    target.candidate_id, target.result_digest]);
}

export function seriesPath(target: SeriesTarget): string {
  const query = new URLSearchParams({attempt_id:target.attempt_id, result_digest:target.result_digest});
  return `/runs/${encodeURIComponent(target.run_id)}/candidates/${encodeURIComponent(target.candidate_id)}/series?${query}`;
}

export function assertSeriesTarget(series: CandidateSeriesResponse, target: SeriesTarget): void {
  if (series.integrity !== "verified" || seriesTargetKey(series) !== seriesTargetKey(target)) {
    throw new Error("图表结果与当前审核目标不一致，请刷新实验后重试。");
  }
}

/** Read computed values only; plotting never reconstructs or fills missing returns. */
export function chartPoints(series: Pick<CandidateSeriesResponse, "points">, metric: SeriesMetric): ChartPoint[] {
  const returns = metric === "gross_return" || metric === "cumulative_gross_return";
  return series.points.map(point => ({
    date: returns ? point.exit_date : point.signal_date,
    value: typeof point[metric] === "number" && Number.isFinite(point[metric]) ? point[metric] : null,
    signalDate: point.signal_date,
  }));
}

/** Calendar spacing, visible true zeros, and null-separated line segments. */
export function plotSeries(points: ChartPoint[], bounds?: readonly [number, number]) {
  const left=62, right=596, top=14, bottom=142;
  const dated=points.flatMap(point => point.date && Number.isFinite(Date.parse(point.date)) ? [Date.parse(point.date)] : []);
  const values=points.flatMap(point => point.value !== null && Number.isFinite(point.value) ? [point.value] : []);
  const first=dated.length ? Math.min(...dated) : null, last=dated.length ? Math.max(...dated) : null;
  let min=bounds?.[0] ?? Math.min(0, ...values), max=bounds?.[1] ?? Math.max(0, ...values);
  if (min === max) { min-=0.01; max+=0.01; }
  const y=(value:number)=> bottom-(value-min)/(max-min)*(bottom-top);
  const segments:PlotPoint[][]=[];
  let current:PlotPoint[]=[];
  const flush=()=>{if(current.length)segments.push(current);current=[];};
  for(const point of points) {
    const date=point.date ? Date.parse(point.date) : NaN;
    if(point.value === null || !Number.isFinite(point.value) || !Number.isFinite(date) || first === null || last === null) {flush();continue;}
    current.push({...point,x:first===last?(left+right)/2:left+(date-first)/(last-first)*(right-left),y:y(point.value)});
  }
  flush();
  return {segments,min,max,first,last,left,right,top,bottom,zero:y(0)};
}

export function seriesValue(value: number | null, percent = true): string {
  return value === null || !Number.isFinite(value) ? "—" : percent ? `${(value*100).toFixed(4)}%` : value.toFixed(6);
}

export function pointExplanation(point: CandidateSeriesResponse["points"][number]): string {
  const reason:Record<string,string>={
    label_would_cross_split_boundary:"持有期将跨越评估边界，未计算覆盖与收益",
    insufficient_finite_assets:"有效因子资产不足",
    constant_factor:"因子横截面为常量，不能形成排名组合",
  };
  if(point.reason) return reason[point.reason] || point.reason;
  return point.rank_ic_state === "constant_forward_returns" ? "后续收益为常量，Rank IC 未定义；毛收益仍有效" : "已评估";
}
