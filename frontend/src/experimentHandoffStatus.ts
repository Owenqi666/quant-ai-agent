import type { PublicExperiment } from "./generated/api-contract";

export type ExperimentHandoffKind = "manual" | "background" | "completed" | "stopped" | "unknown";
export interface ExperimentHandoffStatus {
  kind: ExperimentHandoffKind;
  poll: boolean;
  reasons: string[];
}
const terminalJobs = new Set(["completed", "blocked", "failed", "cancelled", "exhausted"]);
const stoppedSessions = new Set(["stopped", "cancelled", "exhausted", "interrupted"]);

/** Project server-owned states; never infer completion from the run alone or retry a write. */
export function experimentHandoffStatus(experiment: PublicExperiment): ExperimentHandoffStatus {
  const { job, reservation, session } = experiment;
  const reasons = [...new Set([job?.stop_reason, session.stop_reason, reservation.outcome?.code,
    reservation.outcome?.message].filter((value): value is string => typeof value === "string" && !!value))];
  if (experiment.session_id !== session.id || reservation.action !== "experiment" ||
      (job && reservation.children?.research_job_id !== job.id) ||
      (!job && !!reservation.children?.research_job_id)) {
    return { kind: "unknown", poll: false, reasons: ["EXPERIMENT_IDENTITY_MISMATCH", ...reasons] };
  }
  if (reservation.status === "finished") {
    if (!reservation.outcome || reservation.outcome.status === "interrupted" ||
        reservation.outcome.code === "OUTCOME_UNKNOWN") {
      return { kind: "unknown", poll: false, reasons };
    }
    if (job?.state === "completed" && reservation.outcome.status === "completed") {
      return { kind: "completed", poll: false, reasons };
    }
    return { kind: reservation.outcome.status === "failed" ? "stopped" : "unknown", poll: false, reasons };
  }
  const background = !!job && (job.state === "submitted" || job.state === "observed" || terminalJobs.has(job.state));
  if (background) return { kind: "background", poll: true, reasons };
  if (stoppedSessions.has(session.effective_state)) return { kind: "stopped", poll: false, reasons };
  return { kind: "manual", poll: false, reasons };
}
