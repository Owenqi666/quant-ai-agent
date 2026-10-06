import { describe, expect, it } from "vitest";
import { initialReviewDraft, isReviewDraft, loadReviewDraft, readMutation, recoveryKey, removeDurably, saveReviewDraft, stageMutation } from "./reviewRecovery";
const storage = () => {
  const values = new Map<string, string>();
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
};
describe("durable review recovery boundaries", () => {
  it("isolates every frozen output identity, including ambiguous delimiter values", () => {
    const original = recoveryKey("workspace", "review-draft", "run", "candidate", "attempt", "digest");
    for (const args of [["other", "run", "candidate", "attempt", "digest"], ["workspace", "other", "candidate", "attempt", "digest"], ["workspace", "run", "other", "attempt", "digest"], ["workspace", "run", "candidate", "other", "digest"], ["workspace", "run", "candidate", "attempt", "other"]]) {
      expect(recoveryKey(args[0], "review-draft", ...args.slice(1))).not.toBe(original);
    }
    expect(recoveryKey("a:b", "c")).not.toBe(recoveryKey("a", "b:c"));
  });
  it("round trips actual reasons/source and only completed intervals with interrupted-clock marker", () => {
    const s = storage(), key = recoveryKey("w", "review-draft", "r", "c", "a", "d");
    const draft = initialReviewDraft("automation", true);
    draft.note = "Fixture, not human research"; draft.assessment.reviewer = "automation";
    draft.assessment.dimensions.evidence_accuracy.reason = "Evidence remains unassessed";
    draft.assessment.active_intervals = [{ started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-01T00:01:00Z" }];
    draft.timerWasRunning = true;
    saveReviewDraft(s, key, draft);
    const recovered = loadReviewDraft(s, key)!.value;
    expect(recovered).toEqual(draft);
    expect(recovered.assessment.dimensions.evidence_accuracy.outcome).toBe("not_assessed");
    expect(recovered.assessment.active_intervals).toHaveLength(1);
    expect(loadReviewDraft(s, key + "changed")).toBeNull();
  });
  it("rejects unknown schemas, malformed judgments and mismatched draft scopes", () => {
    const s = storage();
    s.setItem("key", JSON.stringify({ version: 2, scope: "key", savedAt: "now", value: initialReviewDraft() }));
    expect(() => loadReviewDraft(s, "key")).toThrow();
    saveReviewDraft(s, "key", initialReviewDraft());
    const raw = JSON.parse(s.getItem("key")!); raw.scope = "different"; s.setItem("key", JSON.stringify(raw));
    expect(() => loadReviewDraft(s, "key")).toThrow();
    const bad = initialReviewDraft(); (bad.assessment.dimensions.evidence_accuracy as {outcome:string}).outcome = "unknown";
    expect(isReviewDraft(bad)).toBe(false);
  });
  it("freezes a request before sending and cannot replace an uncertain operation", () => {
    const s = storage(); const body = { candidate_id: "c", assessment: { reviewer: "original" } };
    const request = stageMutation(s, "key", "/reviews", body, "operation-1");
    body.assessment.reviewer = "edited";
    expect(request.body.assessment).toEqual({ reviewer: "original" });
    expect(readMutation(s, "key", "/reviews")).toEqual(request);
    expect(() => stageMutation(s, "key", "/reviews", {}, "new-key")).toThrow(/待确认/);
    expect(() => readMutation(s, "key", "/other-operation")).toThrow(/作用域/);
    removeDurably(s, "key");
    expect(stageMutation(s, "key", "/reviews", body, "operation-2").body.idempotency_key).toBe("operation-2");
  });
  it("does not claim durability when storage rejects or silently drops a write", () => {
    const unavailable = { getItem: () => null, setItem: () => { throw new Error("quota exceeded"); } };
    expect(() => stageMutation(unavailable, "key", "/reviews", {}, "id")).toThrow(/quota/);
    const dropped = { getItem: () => null, setItem: () => {} };
    expect(() => stageMutation(dropped, "key", "/reviews", {}, "id")).toThrow(/确认本机保存/);
    expect(() => removeDurably({ getItem: () => "retained", removeItem: () => {} }, "key")).toThrow(/未能清除/);
  });
});
