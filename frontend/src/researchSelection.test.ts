import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import type { Research } from "./domain";
import { browserSelectionStorage, persistResearchSelection, researchSelectionKey, resolveResearchSelection, savedResearchSelection } from "./researchSelection";

const research = (id: string): Research => ({ id, title: id, paper_id: "paper", dataset_id: "data", latest_revision_id: "v1", created_at: "now" });
afterEach(() => vi.unstubAllGlobals());
describe("workspace-scoped research selection", () => {
  it("uses the legacy selection only when this workspace has no saved entry", () => {
    const rows = new Map([["paper-alpha-research", "legacy"], [researchSelectionKey("a"), "own"], [researchSelectionKey("empty"), ""]]);
    const storage = { getItem: (key: string) => rows.get(key) ?? null };
    expect(savedResearchSelection(storage, "a")).toBe("own");
    expect(savedResearchSelection(storage, "b")).toBe("legacy");
    expect(savedResearchSelection(storage, "empty")).toBe("");
  });
  it("verifies a valid historical selection outside the first fifty records", async () => {
    const lookup = vi.fn(async (id: string) => research(id));
    const result = await resolveResearchSelection("historical", Array.from({ length: 50 }, (_, i) => research(`row-${i}`)), lookup);
    expect(result.research?.id).toBe("historical");
    expect(result.fellBack).toBe(false);
    expect(lookup).toHaveBeenCalledTimes(1);
  });
  it("falls back only after a verified 404 and handles empty workspaces", async () => {
    const lookup = vi.fn(async (id: string) => { if (id === "missing") throw new ApiError("Not found", 404); return research(id); });
    expect((await resolveResearchSelection("missing", [research("current")], lookup)).research?.id).toBe("current");
    expect((await resolveResearchSelection("missing", [], lookup)).research).toBeNull();
  });
  it("preserves a failed selection for network, server and malformed-response failures", async () => {
    for (const error of [new ApiError("offline", 0), new ApiError("server", 500), new ApiError("bad contract", 404, true)]) {
      const lookup = vi.fn(async () => { throw error; });
      await expect(resolveResearchSelection("keep", [research("fallback")], lookup)).rejects.toBe(error);
      expect(lookup).toHaveBeenCalledTimes(1);
    }
  });
  it("does not treat an HTML or damaged JSON 404 as confirmation that a research is absent", async () => {
    for (const body of ["<html>proxy error</html>", "{broken", ""]) {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 404 })));
      const lookup = vi.fn((id: string) => api<Research>(`/researches/${id}`));
      await expect(resolveResearchSelection("keep", [research("fallback")], lookup)).rejects.toMatchObject({ status: 404, contractViolation: true });
      expect(lookup).toHaveBeenCalledTimes(1);
    }
  });
  it("writes only the workspace key and tolerates unavailable browser storage", () => {
    const setItem = vi.fn();
    persistResearchSelection({ setItem }, "workspace", "confirmed");
    expect(setItem).toHaveBeenCalledWith(researchSelectionKey("workspace"), "confirmed");
    expect(savedResearchSelection({ getItem: () => { throw new Error("blocked"); } }, "workspace")).toBe("");
    expect(() => persistResearchSelection({ setItem: () => { throw new Error("blocked"); } }, "workspace", "confirmed")).not.toThrow();
  });
  it("keeps startup usable when even obtaining localStorage throws SecurityError", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", { configurable: true, get: () => { throw new DOMException("Storage disabled", "SecurityError"); } });
    try {
      const storage = browserSelectionStorage();
      expect(storage).toBeNull();
      const candidate = savedResearchSelection(storage, "workspace");
      const result = await resolveResearchSelection(candidate, [research("first")], async (id) => research(id));
      expect(result.research?.id).toBe("first");
      expect(() => persistResearchSelection(storage, "workspace", "first")).not.toThrow();
    } finally {
      if (descriptor) Object.defineProperty(globalThis, "localStorage", descriptor);
      else Reflect.deleteProperty(globalThis, "localStorage");
    }
  });
});
