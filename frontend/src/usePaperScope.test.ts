import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Paper } from "./domain";

// A deterministic hook scheduler exercises render-before-effect and late-promise
// ordering without installing a DOM renderer. Browser integration remains a
// separate acceptance check; this harness does not claim to emulate React DOM.
type HookSlot = { kind: "state" | "memo" | "effect"; value?: unknown; deps?: readonly unknown[]; cleanup?: () => void };
const hooks = vi.hoisted(() => {
  let cursor = 0;
  const slots: HookSlot[] = [];
  const effects: (() => void)[] = [];
  const same = (a?: readonly unknown[], b?: readonly unknown[]) => !!a && !!b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  return {
    begin: () => { cursor = 0; },
    commit: () => { while (effects.length) effects.shift()!(); },
    unmount: () => { for (const slot of slots) slot.cleanup?.(); },
    reset: () => { slots.length = 0; effects.length = 0; cursor = 0; },
    useState: <T>(initial: T | (() => T)): [T, (value: T | ((previous: T) => T)) => void] => {
      const index = cursor++;
      if (!slots[index]) slots[index] = { kind: "state", value: typeof initial === "function" ? (initial as () => T)() : initial };
      return [slots[index].value as T, (next) => { slots[index].value = typeof next === "function" ? (next as (previous: T) => T)(slots[index].value as T) : next; }];
    },
    useMemo: <T>(factory: () => T, deps: readonly unknown[]): T => {
      const index = cursor++;
      if (!slots[index] || !same(slots[index].deps, deps)) slots[index] = { kind: "memo", deps, value: factory() };
      return slots[index].value as T;
    },
    useEffect: (effect: () => void | (() => void), deps?: readonly unknown[]) => {
      const index = cursor++, previous = slots[index];
      if (!previous || !same(previous.deps, deps)) {
        const next: HookSlot = { kind: "effect", deps }; slots[index] = next;
        effects.push(() => { previous?.cleanup?.(); const cleanup = effect(); if (cleanup) next.cleanup = cleanup; });
      }
    },
  };
});
const apiMock = vi.hoisted(() => vi.fn());
vi.mock("react", async (original) => ({ ...await original<typeof import("react")>(), useState: hooks.useState, useMemo: hooks.useMemo, useEffect: hooks.useEffect }));
vi.mock("./api", () => ({ api: apiMock }));
import { usePaper } from "./components/TaskEditor";

type Request = { path: string; signal: AbortSignal; resolve: (paper: Paper) => void; reject: (error: Error) => void };
let requests: Request[] = [];
const paper = (id: string, title = id): Paper => ({ id, title, sha256: "a".repeat(64), pages: [{ page: 1, text: `${title} source text` }] });
function render(paperId: string, scope?: string, commit = true) {
  hooks.begin(); const output = usePaper(paperId, scope); if (commit) hooks.commit(); return output;
}
async function settle() { await Promise.resolve(); await Promise.resolve(); }
beforeEach(() => {
  hooks.reset(); requests = []; apiMock.mockReset();
  apiMock.mockImplementation((path: string, options: { signal: AbortSignal }) => new Promise<Paper>((resolve, reject) => { requests.push({ path, signal: options.signal, resolve, reject }); }));
});
afterEach(() => hooks.unmount());

describe("paper loading is bound to workspace and paper identity", () => {
  it("keeps the original one-argument API, encodes identifiers, and loads source pages", async () => {
    expect(render("paper /1")).toEqual({ paper: null, error: "" });
    expect(requests[0].path).toBe("/papers/paper%20%2F1");
    const source = paper("paper /1"); requests[0].resolve(source); await settle();
    expect(render("paper /1")).toEqual({ paper: source, error: "" });
    expect(requests).toHaveLength(1);
  });
  it("hides a same-ID paper immediately on workspace change, then aborts the old request", async () => {
    render("shared-paper", "workspace-a");
    requests[0].resolve(paper("shared-paper", "A")); await settle();
    expect(render("shared-paper", "workspace-a").paper?.title).toBe("A");
    expect(render("shared-paper", "workspace-b", false)).toEqual({ paper: null, error: "" });
    hooks.commit(); expect(requests[0].signal.aborted).toBe(true);
    requests[1].resolve(paper("shared-paper", "B")); await settle();
    expect(render("shared-paper", "workspace-b").paper?.title).toBe("B");
  });
  it("does not leak an error on either workspace or paper changes, even before effects run", async () => {
    render("first", "workspace-a"); requests[0].reject(new Error("workspace-a error")); await settle();
    expect(render("first", "workspace-a").error).toContain("workspace-a error");
    expect(render("first", "workspace-b", false)).toEqual({ paper: null, error: "" }); hooks.commit();
    requests[1].reject(new Error("first-paper error")); await settle();
    expect(render("first", "workspace-b").error).toContain("first-paper error");
    expect(render("second", "workspace-b", false)).toEqual({ paper: null, error: "" }); hooks.commit();
    expect(requests[1].signal.aborted).toBe(true);
  });
  it("ignores late results and errors after A to B to A, including transports that ignore abort", async () => {
    render("same", "A"); render("same", "B"); render("same", "A");
    expect(requests[0].signal.aborted).toBe(true); expect(requests[1].signal.aborted).toBe(true);
    requests[2].resolve(paper("same", "new A")); await settle();
    requests[0].resolve(paper("same", "stale A")); requests[1].reject(new Error("stale B")); await settle();
    expect(render("same", "A")).toEqual({ paper: paper("same", "new A"), error: "" });
    expect(requests).toHaveLength(3);
  });
  it("clears an empty selection without fetching and aborts when unmounted", async () => {
    render("selected", "A"); requests[0].reject(new Error("old error")); await settle();
    expect(render("", "A", false)).toEqual({ paper: null, error: "" }); hooks.commit();
    expect(requests).toHaveLength(1); expect(requests[0].signal.aborted).toBe(true);
    render("next", "A"); hooks.unmount(); expect(requests[1].signal.aborted).toBe(true);
    requests[1].resolve(paper("next")); await settle();
  });
  it("does not expose a response with a mismatched paper identifier", async () => {
    render("expected", "A"); requests[0].resolve(paper("unexpected")); await settle();
    expect(render("expected", "A").paper).toBeNull();
  });
});
