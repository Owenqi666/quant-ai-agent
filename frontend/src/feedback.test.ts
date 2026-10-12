import { describe, expect, it } from "vitest";
import { catalogPath, validateDecision, matchesResolutionReview } from "./feedback";
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
    expect(() => validateDecision("resolved", "implementation_fix", "Verified fix", "revision", "run", "check", "new-accepted-review")).not.toThrow();
    expect(() => validateDecision("deferred", "accept_limitation", "Missing authorized data", "", "", "")).not.toThrow();
  });
  it("lets the server enforce exact attempt and case compatibility rather than inventing a pass", () => {
    // Presence alone permits submission; it never produces a closure or positive check result.
    expect(validateDecision("awaiting_review", "implementation_fix", "Needs independent check", "revision", "run", "")).toBeUndefined();
    expect(validateDecision("resolved", "hypothesis_change", "Explicit new hypothesis judgment", "revision", "run", "", "new-accepted-review")).toBeUndefined();
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

import type { Review, Run } from "./domain";
import type { Issue } from "./feedback";
describe("exact follow-up review selection", () => {
  const review: Review = { id:"new-review", run_id:"new-run",revision_id:"v2",attempt_id:"a2",candidate_id:"c",result_digest:"candidate-digest",verdict:"accepted",category:"evidence",note:"Checked new evidence",source:"automation",created_at:"2026-10-08" };
  const issue: Issue = { id:"i",review_id:"original-review",research_id:"r",candidate_id:"c",created_at:"2026-10-08",source:"automation",state:"awaiting_review",latest_event_id:"e",events:[] };
  const run: Run = {id:"new-run",revision_id:"v2",research_id:"r",status:"completed",mode:"fixed",created_at:"2026-10-08",attempt_count:1,verification:{verified:true},review_targets:[{candidate_id:"c",attempt_id:"a2",result_digest:"candidate-digest"}]};
  it("requires a separate acceptance even for evidence updates", () => {
    expect(()=>validateDecision("resolved","evidence_update","Reviewed evidence","v2","new-run","")).toThrow("接受复审");
    expect(()=>validateDecision("resolved","evidence_update","Reviewed evidence","v2","new-run","","new-review")).not.toThrow();
    expect(matchesResolutionReview(review,issue,run)).toBe(true);
    for (const changed of [{id:"original-review"},{run_id:"old-run"},{attempt_id:"old"},{result_digest:"state-digest"},{candidate_id:"wrong"},{verdict:"needs_changes"}]) expect(matchesResolutionReview({...review,...changed},issue,run)).toBe(false);
    expect(matchesResolutionReview({...review,verdict:"needs_changes"},issue,run,false)).toBe(true);
    expect(matchesResolutionReview(review,issue,{...run,verification:{verified:false}})).toBe(false);
  });
  it("uses review linkage only on the issue catalog", () => {
    const filters={research_id:"r",review_id:"review&unsafe=true"};
    expect(new URL(catalogPath("issues",filters),"http://local").searchParams.get("review_id")).toBe("review&unsafe=true");
    expect(new URL(catalogPath("runs",filters),"http://local").searchParams.has("review_id")).toBe(false);
  });
});
