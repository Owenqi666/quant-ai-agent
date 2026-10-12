import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import type { Research, Revision, Run } from "./domain";

const harness = vi.hoisted(() => ({
  refs: [] as { current: unknown }[], cursor: 0, state: null as unknown, updates: 0,
  api: vi.fn(),
}));
// Exercise the actual captured callback bodies without a browser. Scheduling and
// component integration remain covered by the application's browser tests.
vi.mock("react", async () => ({
  ...await vi.importActual<typeof import("react")>("react"),
  useRef: (initial: unknown) => {
    const index = harness.cursor++;
    if (!harness.refs[index]) harness.refs[index] = { current: initial };
    return harness.refs[index];
  },
  useState: (initial: unknown) => {
    if (harness.state === null) harness.state = initial;
    return [harness.state, (next: unknown) => {
      harness.updates += 1;
      harness.state = typeof next === "function" ? next(harness.state) : next;
    }];
  },
  useCallback: (callback: unknown) => callback,
  useEffect: () => {},
}));
vi.mock("./api", async () => ({ ...await vi.importActual<typeof import("./api")>("./api"), api: harness.api }));

import { useWorkPosition } from "./useWorkPosition";
import { saveWorkPosition } from "./workPosition";
import { WorkspaceRequestScope } from "./workspaceScope";

const research = (id: string): Research => ({ id, title: id, paper_id: "paper", dataset_id: "data",
  created_at: "now", latest_revision_id: "revision",
  revisions: [{ id: "revision", research_id: id, number: 1 } as Revision] });
const run = (research_id: string): Run => ({ id: "run", research_id, revision_id: "revision",
  status: "completed", mode: "normalized_fixed", created_at: "now", attempt_count: 1 });
const position = { stage: "review" as const, revisionId: "revision", runId: "run" };
const render = (scope: WorkspaceRequestScope, researchId: string) => {
  harness.cursor = 0;
  return useWorkPosition({ workspaceScope: scope, researchId });
};
beforeEach(() => {
  harness.refs = []; harness.cursor = 0; harness.state = null; harness.updates = 0; harness.api.mockReset();
  const rows = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => rows.get(key) ?? null,
    setItem: (key: string, value: string) => { rows.set(key, value); } });
});
afterEach(() => vi.unstubAllGlobals());

describe("stale work-position callback entry", () => {
  it("does not let a previous research callback cancel or clear a newer restore", async () => {
    const scope = new WorkspaceRequestScope();
    scope.acceptHealthRequest(scope.beginHealthRequest(), "workspace");
    saveWorkPosition(localStorage, "workspace", "b", position);
    const old = render(scope, "a"), current = render(scope, "b");
    let resolve!: (value: Run) => void;
    harness.api.mockImplementation(() => new Promise<Run>(done => { resolve = done; }));
    const restoring = current.restore(research("b"));
    const updates = harness.updates;
    expect((await old.restore(research("a"))).status).toBe("stale");
    old.cancel();
    expect(harness.updates).toBe(updates);
    expect(harness.api).toHaveBeenCalledExactlyOnceWith("/runs/run");
    resolve(run("b"));
    expect((await restoring).status).toBe("restored");
    expect(current.save(position)).toBe(true);
    expect(old.save(position)).toBe(false);
  });
  it("checks the live workspace epoch even before a new render updates liveKey", async () => {
    const scope = new WorkspaceRequestScope();
    scope.acceptHealthRequest(scope.beginHealthRequest(), "old");
    const old = render(scope, "study");
    scope.acceptHealthRequest(scope.beginHealthRequest(), "new");
    const updates = harness.updates;
    expect((await old.restore(research("study"))).status).toBe("stale");
    old.cancel();
    expect(old.save(position)).toBe(false);
    expect(harness.updates).toBe(updates);
    expect(harness.api).not.toHaveBeenCalled();
  });
  it("still lets an explicit current-scope navigation invalidate an active restore", async () => {
    const scope = new WorkspaceRequestScope();
    scope.acceptHealthRequest(scope.beginHealthRequest(), "workspace");
    saveWorkPosition(localStorage, "workspace", "study", position);
    const current = render(scope, "study");
    let resolve!: (value: Run) => void;
    harness.api.mockImplementation(() => new Promise<Run>(done => { resolve = done; }));
    const restoring = current.restore(research("study"));
    current.cancel();
    resolve(run("study"));
    expect((await restoring).status).toBe("stale");
    expect(current.save({ ...position, runId: "", stage: "materials" })).toBe(true);
  });
});
