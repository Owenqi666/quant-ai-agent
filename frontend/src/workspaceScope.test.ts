import { describe, expect, it } from "vitest";
import { WorkspaceRequestScope } from "./workspaceScope";

const confirm = (scope: WorkspaceRequestScope, workspace: string | null) => scope.acceptHealthRequest(scope.beginHealthRequest(), workspace);

describe("confirmed workspace request scope", () => {
  it("retains a confirmed identity and in-flight scope during a temporary disconnect", () => {
    const scope = new WorkspaceRequestScope();
    confirm(scope, "workspace-a");
    const snapshot = scope.capture(), key = scope.key;
    confirm(scope, null);
    expect(scope.workspaceId).toBe("workspace-a");
    expect(scope.key).toBe(key);
    expect(scope.isCurrent(snapshot)).toBe(true);
  });

  it("discards a late health response, including a late disconnection", () => {
    const scope = new WorkspaceRequestScope();
    const old = scope.beginHealthRequest(), latest = scope.beginHealthRequest();
    expect(scope.acceptHealthRequest(latest, "workspace-b")).toBe(true);
    expect(scope.acceptHealthRequest(old, "workspace-a")).toBe(false);
    expect(scope.acceptHealthRequest(old, null)).toBe(false);
    expect(scope.workspaceId).toBe("workspace-b");
  });

  it("invalidates the original request even after an A to B to A switch", () => {
    const scope = new WorkspaceRequestScope();
    confirm(scope, "workspace-a");
    const original = scope.capture(), originalKey = scope.key;
    confirm(scope, "workspace-b");
    expect(scope.isCurrent(original)).toBe(false);
    confirm(scope, "workspace-a");
    expect(scope.isCurrent(original)).toBe(false);
    expect(scope.key).not.toBe(originalKey);
    expect(scope.isCurrent(scope.capture())).toBe(true);
  });

  it("keeps same-workspace refreshes valid but rejects aborted selections", () => {
    const scope = new WorkspaceRequestScope();
    confirm(scope, "workspace-a");
    const snapshot = scope.capture(), controller = new AbortController();
    confirm(scope, "workspace-a");
    expect(scope.isCurrent(snapshot, controller.signal)).toBe(true);
    controller.abort();
    expect(scope.isCurrent(snapshot, controller.signal)).toBe(false);
  });
});
