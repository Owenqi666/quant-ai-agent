import { describe, expect, it } from "vitest";
import type { PublicExperiment, ResearchJobSummary, SessionOutcome } from "./generated/api-contract";
import { experimentHandoffStatus } from "./experimentHandoffStatus";

function experiment(state: ResearchJobSummary["state"] = "submitted", finished = false,
  outcome: SessionOutcome | null = null): PublicExperiment {
  return { id: "experiment", session_id: "session", job: { id: "job", state, stop_reason: null },
    session: { id: "session", effective_state: "active", stop_reason: null },
    reservation: { action: "experiment", status: finished ? "finished" : "effect_recorded", outcome,
      children: { research_job_id: "job" } } } as PublicExperiment;
}
describe("server-owned experiment handoff state", () => {
  it.each(["created", "validated", "committed"] as const)("%s still needs explicit submission", state => {
    expect(experimentHandoffStatus(experiment(state))).toMatchObject({ kind: "manual", poll: false });
  });
  it.each(["submitted", "observed", "completed", "blocked", "failed", "cancelled", "exhausted"] as const)(
    "%s with an unfinished reservation needs only background readback", state => {
      expect(experimentHandoffStatus(experiment(state))).toMatchObject({ kind: "background", poll: true });
    });
  it("requires both completed job and the completed finished reservation", () => {
    expect(experimentHandoffStatus(experiment("completed", true, { status: "completed", code: "EXPERIMENT_COMPLETED" })))
      .toMatchObject({ kind: "completed", poll: false });
    expect(experimentHandoffStatus(experiment("submitted", true, { status: "completed", code: "EXPERIMENT_COMPLETED" })))
      .toMatchObject({ kind: "unknown", poll: false });
    expect(experimentHandoffStatus(experiment("completed", true, null))).toMatchObject({ kind: "unknown", poll: false });
  });
  it.each(["blocked", "failed", "cancelled", "exhausted"] as const)("preserves the stopped %s outcome", state => {
    const row = experiment(state, true, { status: "failed", code: "EXPERIMENT_" + state.toUpperCase() });
    row.job!.stop_reason = "ORIGINAL_STOP";
    expect(experimentHandoffStatus(row)).toEqual({ kind: "stopped", poll: false,
      reasons: ["ORIGINAL_STOP", "EXPERIMENT_" + state.toUpperCase()] });
  });
  it("unknown outcome cannot become successful from a completed run or job", () => {
    expect(experimentHandoffStatus(experiment("completed", true, { status: "interrupted", code: "OUTCOME_UNKNOWN" })))
      .toMatchObject({ kind: "unknown", poll: false });
    expect(experimentHandoffStatus(experiment("completed", true, { status: "completed", code: "OUTCOME_UNKNOWN" })))
      .toMatchObject({ kind: "unknown", poll: false });
    expect(experimentHandoffStatus(experiment("completed", true, { status: "awaiting_input", code: "MANUAL_REVIEW" })))
      .toMatchObject({ kind: "unknown", poll: false });
  });
  it("continues GET readback when the parent expires before the child finishes", () => {
    const row = experiment(); row.session.effective_state = "exhausted"; row.session.stop_reason = "SESSION_EXPIRED";
    expect(experimentHandoffStatus(row)).toEqual({ kind: "background", poll: true, reasons: ["SESSION_EXPIRED"] });
    row.job!.state = "cancelled"; row.reservation.status = "finished";
    row.reservation.outcome = { status: "failed", code: "EXPERIMENT_CANCELLED" };
    expect(experimentHandoffStatus(row)).toEqual({ kind: "stopped", poll: false, reasons: ["SESSION_EXPIRED", "EXPERIMENT_CANCELLED"] });
  });
  it("a closed parent does not invite another pre-submit action", () => {
    const row = experiment("created"); row.session.effective_state = "cancelled";
    expect(experimentHandoffStatus(row)).toMatchObject({ kind: "stopped", poll: false });
  });
  it("does not invent a job when none was saved", () => {
    const row = experiment("created"); row.job = null; row.reservation.children = null;
    expect(experimentHandoffStatus(row)).toMatchObject({ kind: "manual", poll: false });
  });
  it("fails closed for a cross-session or cross-job result", () => {
    const row = experiment("completed", true, { status: "completed", code: "EXPERIMENT_COMPLETED" });
    row.session_id = "another-session";
    expect(experimentHandoffStatus(row)).toMatchObject({ kind: "unknown", poll: false, reasons: ["EXPERIMENT_IDENTITY_MISMATCH", "EXPERIMENT_COMPLETED"] });
    row.session_id = "session"; row.reservation.children!.research_job_id = "another-job";
    expect(experimentHandoffStatus(row)).toMatchObject({ kind: "unknown", poll: false });
  });
  it("requires the exact recorded job child before showing completion", () => {
    const row = experiment("completed", true, { status: "completed", code: "EXPERIMENT_COMPLETED" });
    row.reservation.children = null;
    expect(experimentHandoffStatus(row)).toMatchObject({ kind: "unknown", poll: false });
    row.reservation.children = {};
    expect(experimentHandoffStatus(row)).toMatchObject({ kind: "unknown", poll: false });
    row.reservation.children = { research_job_id: "job" };
    expect(experimentHandoffStatus(row)).toMatchObject({ kind: "completed", poll: false });
  });
  it("projects the saved receipt without mutating server-owned data", () => {
    const row = experiment(), before = JSON.stringify(row);
    experimentHandoffStatus(row);
    expect(JSON.stringify(row)).toBe(before);
  });
});
