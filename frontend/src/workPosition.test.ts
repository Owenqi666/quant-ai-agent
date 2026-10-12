import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import type { Research, Revision, Run } from "./domain";
import { readWorkPosition, resolveWorkPosition, saveWorkPosition, WorkPositionRequest, workPositionKey, workPositionNoticeMessage } from "./workPosition";
import type { StoredWorkPosition, WorkPosition } from "./workPosition";
import { WorkspaceRequestScope } from "./workspaceScope";

const revision = (id: string, research_id = "study", candidate = "candidate"): Revision =>
  ({ id, research_id, number: id === "v1" ? 1 : 2, task: { candidates: [{ id: candidate }] } } as Revision);
const research: Research = { id: "study", title: "Study", paper_id: "paper", dataset_id: "data",
  latest_revision_id: "v2", created_at: "now", revisions: [revision("v1"), revision("v2")] };
const position: WorkPosition = { stage: "review", revisionId: "v1", runId: "historical" };
const record = (overrides: Partial<WorkPosition> = {}): StoredWorkPosition =>
  ({ version: 1, workspaceId: "workspace", researchId: "study", position: { ...position, ...overrides } });
const run = (overrides: Partial<Run> = {}): Run => ({ id: "historical", research_id: "study",
  revision_id: "v2", status: "completed", mode: "normalized_fixed", created_at: "now", attempt_count: 1, ...overrides });
const options = (saved: StoredWorkPosition | null = record()) => ({
  workspaceId: "workspace", researchId: "study", research, record: saved, lookupRun: vi.fn(async () => run()),
});
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
};

describe("work-position storage", () => {
  it("isolates workspace and research identities without separator collisions", () => {
    expect(workPositionKey("a:b", "c")).not.toBe(workPositionKey("a", "b:c"));
    const rows = new Map<string, string>();
    const storage = { getItem: (key: string) => rows.get(key) ?? null, setItem: (key: string, value: string) => { rows.set(key, value); } };
    expect(saveWorkPosition(storage, "workspace", "study", position).saved).toBe(true);
    expect(readWorkPosition(storage, "workspace", "study").record?.position).toEqual(position);
    expect(readWorkPosition(storage, "other", "study").record).toBeNull();
    expect(readWorkPosition(storage, "workspace", "other").record).toBeNull();
  });
  it("stores only navigation fields, never injected business or draft snapshots", () => {
    const setItem = vi.fn();
    saveWorkPosition({ setItem }, "workspace", "study", { ...position, state: { metrics: 9 }, note: "draft" } as WorkPosition);
    expect(JSON.parse(setItem.mock.calls[0][1])).toEqual(record());
  });
  it("keeps explicit no-run selection distinct from no saved position", () => {
    const text = JSON.stringify(record({ runId: "", stage: "evidence" }));
    const read = readWorkPosition({ getItem: () => text }, "workspace", "study");
    expect(read.record?.position.runId).toBe("");
    expect(readWorkPosition({ getItem: () => null }, "workspace", "study").record).toBeNull();
  });
  it("declines corrupt, newer-version or scope-spoofed records with visible reasons", () => {
    for (const text of ["broken", "null", JSON.stringify({ ...record(), version: 2 }), JSON.stringify(record({ stage: "fake" as WorkPosition["stage"] }))]) {
      expect(readWorkPosition({ getItem: () => text }, "workspace", "study")).toEqual({ record: null, notices: ["invalid-record"] });
    }
    const text = JSON.stringify({ ...record(), researchId: "foreign" });
    expect(readWorkPosition({ getItem: () => text }, "workspace", "study").notices).toEqual(["scope-mismatch"]);
  });
  it("degrades unavailable or throwing browser storage without stopping navigation", () => {
    for (const storage of [null, { getItem: () => { throw new Error("disabled"); } }]) {
      expect(readWorkPosition(storage, "workspace", "study").notices).toEqual(["storage-unavailable"]);
    }
    expect(saveWorkPosition(null, "workspace", "study", position).saved).toBe(false);
    expect(saveWorkPosition({ setItem: () => { throw new Error("quota"); } }, "workspace", "study", position).notices).toEqual(["storage-unavailable"]);
    expect(workPositionNoticeMessage("storage-unavailable")).toContain("当前仍可操作");
  });
});

describe("verified work-position restoration", () => {
  it("directly reads a saved historical run and keeps editing revision independent", async () => {
    const config = options();
    const result = await resolveWorkPosition(config);
    expect(config.lookupRun).toHaveBeenCalledExactlyOnceWith("historical");
    expect(result.status).toBe("restored");
    expect(result.position).toEqual(position);
    expect(result.run?.revision_id).toBe("v2");
  });
  it("does not select a run for a new or deliberately unselected position", async () => {
    const absent = options(null);
    expect((await resolveWorkPosition(absent)).status).toBe("absent");
    expect(absent.lookupRun).not.toHaveBeenCalled();
    const unselected = options(record({ runId: "" }));
    expect((await resolveWorkPosition(unselected)).position?.runId).toBe("");
    expect(unselected.lookupRun).not.toHaveBeenCalled();
  });
  it("keeps navigation usable without storage and retains the warning", async () => {
    const result = await resolveWorkPosition({ ...options(null), notices: ["storage-unavailable"] });
    expect(result.status).toBe("absent");
    expect(result.notices).toEqual(["storage-unavailable"]);
  });
  it("drops only a run confirmed missing by a valid API 404", async () => {
    const saved = record();
    const lookupRun = vi.fn(async () => { throw new ApiError("Not found", 404); });
    const result = await resolveWorkPosition({ ...options(saved), lookupRun });
    expect(result.status).toBe("degraded");
    expect(result.position).toEqual({ ...position, runId: "" });
    expect(result.notices).toEqual(["run-missing"]);
    expect(saved.position.runId).toBe("historical");
    expect(lookupRun).toHaveBeenCalledTimes(1);
  });
  it.each([new ApiError("Offline", 0), new ApiError("Busy", 500), new ApiError("Bad response", 404, true)])(
    "preserves the saved target on unconfirmed failure %s", async error => {
      const saved = record();
      const lookupRun = vi.fn(async () => { throw error; });
      const result = await resolveWorkPosition({ ...options(saved), lookupRun });
      expect(result.status).toBe("unavailable");
      expect(result.position).toBeNull();
      expect(result.run).toBeNull();
      expect(result.error).toBe(error);
      expect(saved.position).toEqual(position);
      expect(lookupRun).toHaveBeenCalledExactlyOnceWith("historical");
    });
  it.each([{ id: "wrong-id" }, { research_id: "foreign" }, { revision_id: "foreign-version" }])(
    "rejects a saved run with mismatched identity %s", async override => {
      const result = await resolveWorkPosition({ ...options(), lookupRun: async () => run(override) });
      expect(result.status).toBe("degraded");
      expect(result.position?.runId).toBe("");
      expect(result.run).toBeNull();
      expect(result.notices).toContain("run-mismatch");
    });
  it("rejects a revision listed under the wrong research and never falls into its data", async () => {
    const badResearch = { ...research, revisions: [revision("v1"), revision("v2", "foreign")] };
    const result = await resolveWorkPosition({ ...options(), research: badResearch });
    expect(result.run).toBeNull();
    expect(result.notices).toContain("run-mismatch");
  });
  it("replaces a missing editing version without switching the explicitly selected run", async () => {
    const result = await resolveWorkPosition(options(record({ revisionId: "deleted" })));
    expect(result.position).toEqual({ ...position, revisionId: "v2" });
    expect(result.run?.id).toBe("historical");
    expect(result.notices).toEqual(["revision-missing"]);
  });
  it("verifies record and fetched study identity before issuing any run request", async () => {
    const config = options();
    const result = await resolveWorkPosition({ ...config, research: { ...research, id: "other" } });
    expect(result.status).toBe("unavailable");
    expect(config.lookupRun).not.toHaveBeenCalled();
  });
  it("restores a candidate against the frozen run version rather than the editing version", async () => {
    const result = await resolveWorkPosition({ ...options(record({ candidateId: "frozen" })),
      research: { ...research, revisions: [revision("v1", "study", "editing"), revision("v2", "study", "frozen")] } });
    expect(result.position?.candidateId).toBe("frozen");
    const missing = await resolveWorkPosition(options(record({ candidateId: "other" })));
    expect(missing.position?.candidateId).toBeUndefined();
    expect(missing.notices).toContain("candidate-missing");
  });
  it("restores issue IDs only after checking issue and study identity", async () => {
    const lookupIssue = vi.fn(async () => ({ id: "issue", research_id: "study", candidate_id: "candidate" }));
    const saved = record({ issueId: "issue", candidateId: "candidate" });
    const valid = await resolveWorkPosition({ ...options(saved), lookupIssue });
    expect(valid.position?.issueId).toBe("issue");
    expect(lookupIssue).toHaveBeenCalledExactlyOnceWith("issue");
    for (const override of [{ id: "other" }, { research_id: "other" }, { candidate_id: "other" }]) {
      const invalid = await resolveWorkPosition({ ...options(saved), lookupIssue: async () => ({ id: "issue", research_id: "study", candidate_id: "candidate", ...override }) });
      expect(invalid.position?.issueId).toBeUndefined();
      expect(invalid.notices).toContain("issue-mismatch");
    }
  });
  it("separates deleted issues from network failure without mutating the original navigation", async () => {
    const saved = record({ issueId: "issue" });
    const gone = await resolveWorkPosition({ ...options(saved), lookupIssue: async () => { throw new ApiError("gone", 404); } });
    expect(gone.status).toBe("degraded");
    expect(gone.position?.issueId).toBeUndefined();
    const offline = await resolveWorkPosition({ ...options(saved), lookupIssue: async () => { throw new ApiError("offline", 0); } });
    expect(offline.status).toBe("unavailable");
    expect(offline.position).toBeNull();
    expect(saved.position.issueId).toBe("issue");
  });
});

describe("stale navigation guards", () => {
  it("ignores a slow response after the user explicitly chooses a different position", async () => {
    const pending = deferred<Run>();
    const guard = new WorkPositionRequest();
    const restoring = resolveWorkPosition({ ...options(), lookupRun: () => pending.promise, isCurrent: guard.begin(() => true) });
    guard.cancel();
    pending.resolve(run());
    expect((await restoring).status).toBe("stale");
  });
  it("ignores an older restore after a second restore and does not request its issue", async () => {
    const pending = deferred<Run>(), guard = new WorkPositionRequest();
    const lookupIssue = vi.fn();
    const first = resolveWorkPosition({ ...options(record({ issueId: "issue" })), lookupRun: () => pending.promise, lookupIssue, isCurrent: guard.begin(() => true) });
    const second = await resolveWorkPosition({ ...options(record({ runId: "" })), isCurrent: guard.begin(() => true) });
    pending.resolve(run());
    expect(second.status).toBe("restored");
    expect((await first).status).toBe("stale");
    expect(lookupIssue).not.toHaveBeenCalled();
  });
  it("rejects a response even after switching away and back to the same workspace identity", async () => {
    const scope = new WorkspaceRequestScope();
    scope.acceptHealthRequest(scope.beginHealthRequest(), "workspace");
    const captured = scope.capture(), pending = deferred<Run>();
    const result = resolveWorkPosition({ ...options(), lookupRun: () => pending.promise, isCurrent: () => scope.isCurrent(captured) });
    scope.acceptHealthRequest(scope.beginHealthRequest(), "other");
    scope.acceptHealthRequest(scope.beginHealthRequest(), "workspace");
    pending.resolve(run());
    expect((await result).status).toBe("stale");
  });
});
