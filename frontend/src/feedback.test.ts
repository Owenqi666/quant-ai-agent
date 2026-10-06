import { describe, expect, it } from "vitest";
import { catalogPath, validateDecision } from "./feedback";
import { reviewPayload } from "./domain";

describe("feedback evidence and declared sources", () => {
  it("keeps automated acceptance separate from manually declared review coverage", () => {
    expect(reviewPayload("alpha006", "accepted", "implementation", "Fixture", "automation").source).toBe("automation");
    expect(reviewPayload("alpha006", "accepted", "implementation", "Manual review").source).toBe("human");
  });
  it("requires a written decision and linked result before a problem can be closed", () => {
    expect(() => validateDecision("deferred", "accept_limitation", " ", "", "", "")).toThrow("处理依据");
    expect(() => validateDecision("resolved", "hypothesis_change", "Changed hypothesis", "", "run", "")).toThrow("关联修订");
    expect(() => validateDecision("resolved", "implementation_fix", "Verified fix", "revision", "run", "")).toThrow("回归检查");
    expect(() => validateDecision("resolved", "implementation_fix", "Verified fix", "revision", "run", "check")).not.toThrow();
    expect(() => validateDecision("deferred", "accept_limitation", "Missing authorized data", "", "", "")).not.toThrow();
  });
  it("lets the server enforce exact attempt and case compatibility rather than inventing a pass", () => {
    // Presence alone permits submission; it never produces a closure or positive check result.
    expect(validateDecision("awaiting_review", "implementation_fix", "Needs independent check", "revision", "run", "")).toBeUndefined();
    expect(validateDecision("resolved", "hypothesis_change", "Explicit new hypothesis judgment", "revision", "run", "")).toBeUndefined();
  });
});
describe("bounded catalog filters", () => {
  it("preserves arbitrary gap cursors and encodes user filters without building raw query text", () => {
    const path = catalogPath("reviews", { research_id: "r", candidate_id: "alpha&source=human", source: "automation" }, 725, 900);
    const url = new URL(path, "http://local");
    expect(url.searchParams.get("limit")).toBe("50");
    expect(url.searchParams.get("after")).toBe("725");
    expect(url.searchParams.get("through")).toBe("900");
    expect(url.searchParams.get("candidate_id")).toBe("alpha&source=human");
    expect(url.searchParams.get("source")).toBe("automation");
  });
  it("does not send unsupported stale filters when the selected catalog changes", () => {
    const filters = { source: "human", category: "evidence", status: "open", dataset_id: "data" };
    const research = new URL(catalogPath("researches", filters), "http://local");
    expect(research.searchParams.has("source")).toBe(false);
    expect(research.searchParams.has("category")).toBe(false);
    expect(research.searchParams.has("status")).toBe(false);
    expect(research.searchParams.get("dataset_id")).toBe("data");
    expect(new URL(catalogPath("issues", filters), "http://local").searchParams.get("status")).toBe("open");
  });
});
