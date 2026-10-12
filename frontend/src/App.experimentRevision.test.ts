import { describe, expect, it } from "vitest";
import { resolveResearchRevision } from "./App";
import type { Research, Revision } from "./domain";

function study(id = "study-a"): Research {
  return { id, title: "Navigation scope", paper_id: "paper", dataset_id: "data", created_at: "2026-10-09",
    latest_revision_id: `${id}-v3`, revisions: [1, 2, 3].map(number => ({
      id: `${id}-v${number}`, research_id: id, number,
    } as Revision)) };
}

describe("explicit experiment revision navigation", () => {
  it("selects the actual experiment revision even when latest is a different revision", () => {
    expect(resolveResearchRevision(study(), { researchId: "study-a", revisionId: "study-a-v2" })).toBe("study-a-v2");
  });
  it("waits for requested detail instead of replacing a not-yet-loaded revision with latest", () => {
    const stale = study(); stale.revisions = stale.revisions?.filter(r => r.number !== 2);
    const target = { researchId: stale.id, revisionId: "study-a-v2" };
    expect(resolveResearchRevision(stale, target)).toBeNull();
    expect(resolveResearchRevision(study(), target)).toBe("study-a-v2");
  });
  it("does not let an old study target select a revision in a later selected study", () => {
    expect(resolveResearchRevision(study("study-b"), { researchId: "study-a", revisionId: "study-a-v2" })).toBe("study-b-v3");
  });
  it("rejects a revision object that names the target ID but belongs to another study", () => {
    const value = study(); value.revisions![1] = { ...value.revisions![1], research_id: "study-b" };
    expect(resolveResearchRevision(value, { researchId: "study-a", revisionId: "study-a-v2" })).toBeNull();
  });
  it("does not replace an explicitly empty experiment revision with latest", () => {
    expect(resolveResearchRevision(study(), { researchId: "study-a", revisionId: "" })).toBeNull();
  });
  it("retains default selection for ordinary research navigation or an explicitly cancelled target", () => {
    expect(resolveResearchRevision(study(), null)).toBe("study-a-v3");
    expect(resolveResearchRevision(study(), { researchId: "study-a" })).toBe("study-a-v3");
  });
});
