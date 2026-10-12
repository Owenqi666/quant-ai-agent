import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { CandidateSeriesResponse } from "./generated/api-contract";
import { assertSeriesTarget, chartPoints, plotSeries, pointExplanation, seriesPath, seriesTargetKey, seriesValue } from "./candidateSeries";
import { CandidateSeriesView } from "./components/CandidateCharts";

const series:CandidateSeriesResponse={
  schema_version:1,run_id:"run",research_id:"research",revision_id:"revision",revision_digest:"revision-digest",
  attempt_id:"attempt",candidate_id:"alpha",result_digest:"result-digest",integrity:"verified",status:"evaluated",reason:null,
  source:{artifact_id:"artifact",artifact_name:"candidates/alpha/attempt-1/result.json",artifact_sha256:"export",original_sha256:"original",result_sha256:"result",json_pointer:"/daily"},
  data:{dataset_id:"data",dataset_sha256:"data-digest",version:"fixture",data_kind:"synthetic",universe_size:4},
  evaluation:{split:"validation",start:"2026-01-01",end:"2026-01-08",min_assets:2},
  summary:{sum_gross_return:0.01,mean_gross_return:0.005,mean_rank_ic:0,rank_ic_days:1,evaluated_days:2,skipped_days:1,purged_days:2,factor_coverage:0.5},
  points:[
    {signal_date:"2026-01-01",entry_date:"2026-01-02",exit_date:"2026-01-03",status:"evaluated",reason:null,gross_return:0,cumulative_gross_return:0,rank_ic:0,rank_ic_state:"defined",available_assets:4,coverage:1},
    {signal_date:"2026-01-02",entry_date:"2026-01-03",exit_date:"2026-01-04",status:"skipped",reason:"constant_factor",gross_return:null,cumulative_gross_return:null,rank_ic:null,rank_ic_state:"not_evaluated",available_assets:4,coverage:1},
    {signal_date:"2026-01-03",entry_date:"2026-01-04",exit_date:"2026-01-05",status:"evaluated",reason:null,gross_return:0.01,cumulative_gross_return:0.01,rank_ic:null,rank_ic_state:"constant_forward_returns",available_assets:2,coverage:0.5},
    {signal_date:"2026-01-04",entry_date:null,exit_date:null,status:"purged",reason:"label_would_cross_split_boundary",gross_return:null,cumulative_gross_return:null,rank_ic:null,rank_ic_state:"not_evaluated",available_assets:null,coverage:null},
  ],limitations:["Synthetic fixture only"],
};

describe("candidate series plotting follows immutable computed outputs",()=>{
  it("binds run, revision, attempt, candidate and digest and URL-encodes identifiers",()=>{
    expect(()=>assertSeriesTarget(series,series)).not.toThrow();
    for(const field of ["run_id","revision_id","attempt_id","candidate_id","result_digest"] as const){
      const other={...series,[field]:"changed"};
      expect(seriesTargetKey(other)).not.toBe(seriesTargetKey(series));
      expect(()=>assertSeriesTarget(other,series)).toThrow("不一致");
    }
    expect(seriesPath({...series,candidate_id:"alpha / 1",result_digest:"digest &"})).toBe("/runs/run/candidates/alpha%20%2F%201/series?attempt_id=attempt&result_digest=digest+%26");
  });
  it("plots returns on exit dates and IC/coverage on signal dates without filling nulls",()=>{
    expect(chartPoints(series,"gross_return").map(p=>[p.date,p.value])).toEqual([["2026-01-03",0],["2026-01-04",null],["2026-01-05",0.01],[null,null]]);
    expect(chartPoints(series,"rank_ic").map(p=>[p.date,p.value])).toEqual([["2026-01-01",0],["2026-01-02",null],["2026-01-03",null],["2026-01-04",null]]);
    expect(chartPoints(series,"coverage").map(p=>p.value)).toEqual([1,1,0.5,null]);
  });
  it("keeps a real zero, splits paths at missing days, and uses calendar distance",()=>{
    const plot=plotSeries([{date:"2026-01-01",value:0,signalDate:"a"},{date:"2026-01-02",value:null,signalDate:"b"},{date:"2026-01-04",value:-0.01,signalDate:"c"},{date:"2026-01-05",value:0.01,signalDate:"d"}]);
    expect(plot.segments.map(s=>s.length)).toEqual([1,2]);
    expect(plot.segments[0][0].y).toBe(plot.zero);
    expect(plot.segments[1][0].x).toBe(plot.left+0.75*(plot.right-plot.left));
    expect(plot.segments[1][0].y).toBe(plot.bottom);
    expect(plot.segments[1][1].y).toBe(plot.top);
    expect(seriesValue(0)).toBe("0.0000%");expect(seriesValue(null)).toBe("—");
  });
  it("uses backend cumulative values without compounding or restarting after missing days",()=>{
    const points=structuredClone(series.points);
    points[0].gross_return=0.1;points[0].cumulative_gross_return=0.1;
    points[2].gross_return=-0.03;points[2].cumulative_gross_return=0.07;
    expect(chartPoints({points},"cumulative_gross_return").map(p=>p.value)).toEqual([0.1,null,0.07,null]);
    expect(plotSeries(chartPoints({points},"cumulative_gross_return")).segments).toHaveLength(2);
  });
  it("handles all-null, single-point and constant-zero plots without invalid coordinates",()=>{
    expect(plotSeries([{date:null,value:null,signalDate:"a"}]).segments).toEqual([]);
    const plot=plotSeries([{date:"2026-01-01",value:0,signalDate:"a"}]);
    expect(plot.segments[0][0].x).toBe((plot.left+plot.right)/2);
    expect(Number.isFinite(plot.segments[0][0].y)).toBe(true);
  });
  it("renders accessible daily values, explicit costs/date conventions and nonzero-independent null reasons",()=>{
    const html=renderToStaticMarkup(createElement(CandidateSeriesView,{series}));
    for(const text of ["候选逐日计算明细","不是复利净值或 BRAIN PnL","0.0000%","后续收益为常量","不适用","入场日","退出日","合成数据","未扣交易成本"])expect(html).toContain(text);
    expect(html).toContain('data-date="2026-01-03"');
    expect(html).not.toContain("NaN");
    expect(pointExplanation(series.points[1])).toContain("常量");
    expect(pointExplanation(series.points[3])).toContain("边界");
  });
});
