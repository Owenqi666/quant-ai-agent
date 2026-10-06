import { describe, expect, it } from "vitest";
import { addReportRun, InsightRequestEpoch, insightValue, metricDelta, reportRunLimit } from "./researchInsights";
import type { Run } from "./domain";

const run = (id: string, research = "research-a"): Run => ({ id, research_id: research, revision_id: "revision", status: "completed", mode: "fixed", created_at: "2026-10-01T00:00:00Z", attempt_count: 1 });

describe("research insight selection and truthful values", () => {
  it("rejects cross-research report selections while keeping deliberate selection order", () => {
    const selected = addReportRun([run("first")], run("second"), "research-a");
    expect(selected.map((item) => item.id)).toEqual(["first", "second"]);
    expect(() => addReportRun(selected, run("foreign", "research-b"), "research-a")).toThrow("当前研究");
    expect(selected).toHaveLength(2);
  });
  it("does not duplicate selected runs or silently truncate the declared scope", () => {
    const selected = Array.from({length:reportRunLimit}, (_, i) => run(String(i)));
    expect(addReportRun(selected, selected[0], "research-a")).toBe(selected);
    expect(() => addReportRun(selected, run("overflow"), "research-a")).toThrow("最多选择 20");
    expect(selected).toHaveLength(20);
  });
  it("invalidates late comparison and catalog requests across selection cycles", () => {
    const scope = new InsightRequestEpoch();
    const a = scope.begin();
    scope.invalidate();
    const b = scope.begin();
    expect(scope.isCurrent(a)).toBe(false);
    scope.invalidate();
    const newA = scope.begin();
    expect(scope.isCurrent(a)).toBe(false);
    expect(scope.isCurrent(b)).toBe(false);
    expect(scope.isCurrent(newA)).toBe(true);
  });
  it("keeps missing metrics distinct from zero and never invents a delta", () => {
    expect(insightValue(null)).toBe("未记录");
    expect(insightValue(0)).toBe("0");
    expect(metricDelta(null, true)).toBe("—");
    expect(metricDelta(0, false)).toBe("—");
    expect(metricDelta(0, true)).toBe("0");
    expect(metricDelta(0.2, true)).toBe("+0.2");
    expect(metricDelta(-0.2, true)).toBe("-0.2");
    expect(metricDelta(Infinity, true)).toBe("—");
  });
});

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ComparisonResult } from "./components/ResearchInsights";
import type { ComparisonRun, RunComparison } from "./generated/api-contract";

it("renders an unverified comparison without a misleading numerical improvement", () => {
  const side: ComparisonRun = {
    run: {...run("first"), status:"completed", mode:"fixed", started_at:null, finished_at:null, error:null},
    source_verified:false, verification_error:"Artifact digest did not match", attempt_id:"attempt", state_digest:null,
    revision_digest:"revision-digest", paper_sha256:"paper-digest", dataset_sha256:"dataset-digest", data_metadata:{},
    evaluation:{}, budget:{}, recorded_semantics:null, code_digest:null, environment:null, tool_calls:null, elapsed_seconds:null,
  };
  const comparison: RunComparison = {
    schema_version:1, compared_at:"2026-10-01T00:00:00Z", delta_direction:"candidate_minus_baseline", baseline:side,
    candidate:{...side, run:{...side.run, id:"second"}}, comparable:false,
    conditions:[{name:"recorded_semantics", status:"unknown", blocking:true, baseline:null, candidate:null}],
    candidates:[{candidate_id:"signal", presence:"both", baseline_status:"evaluated", candidate_status:"evaluated",
      baseline_expression:"rank(close)", candidate_expression:"rank(open)", expression_changed:true, comparable:true, reasons:[],
      metrics:[{name:"mean_rank_ic", baseline:null, candidate:17.5, delta:17.5, reason:"Unverified source", baseline_source:null, candidate_source:null}]}],
    limitations:["No human judgment or investment claim"],
  };
  const html = renderToStaticMarkup(createElement(ComparisonResult, {result:comparison}));
  expect(html).toContain("存在不一致或未知条件");
  expect(html).toContain("rank(close)"); expect(html).toContain("rank(open)");
  expect(html).toContain("未记录"); expect(html).toContain("Artifact digest did not match");
  expect(html).toContain("不列指标差值"); expect(html).not.toContain("+17.5");
});
