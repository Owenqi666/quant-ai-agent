import { describe, expect, it } from "vitest";
import { assessmentPayload, emptyAssessment, finishInterval } from "./researchAssessment";

describe("explicit, result-bound research assessment", () => {
  it("does not create positive judgments or measured zero effort from a blank form", () => {
    const draft = emptyAssessment();
    expect(Object.values(draft.dimensions).every((v) => v.outcome === "not_assessed")).toBe(true);
    expect(draft.active_intervals).toEqual([]);
    expect(() => assessmentPayload(draft, undefined, null)).toThrow(/版本标识/);
    expect(() => assessmentPayload(draft, { attempt_id: "a", result_digest: "d" }, null)).toThrow(/审核人/);
  });
  it("retains unknown judgments, reasons and the exact server-issued target", () => {
    const draft = emptyAssessment(); draft.reviewer = " reviewer ";
    for (const dimension of Object.values(draft.dimensions)) dimension.reason = "尚未对该项作判断";
    const result = assessmentPayload(draft, { attempt_id: "old-attempt", result_digest: "old-result" }, null);
    expect(result.expected_attempt_id).toBe("old-attempt");
    expect(result.expected_result_digest).toBe("old-result");
    expect(result.reviewer).toBe("reviewer");
    expect(result.dimensions.evidence_accuracy.outcome).toBe("not_assessed");
  });
  it("requires a paused clock and refuses reverse or zero intervals", () => {
    const draft = emptyAssessment();
    expect(() => assessmentPayload(draft, { attempt_id: "a", result_digest: "d" }, "running")).toThrow(/暂停/);
    expect(() => finishInterval(draft, "2026-10-01T01:00:00Z", "2026-10-01T00:00:00Z")).toThrow();
    const timed = finishInterval(draft, "2026-10-01T00:00:00Z", "2026-10-01T00:01:00Z");
    expect(timed.active_intervals).toHaveLength(1);
    expect(draft.active_intervals).toHaveLength(0);
  });
});
