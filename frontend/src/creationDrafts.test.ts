import { describe, expect, it } from "vitest";
import { creationScope, creationStage, discoverCreationDrafts, discardCreationDraft, isCreationDraft } from "./creationDrafts";
import { draftKey } from "./drafts";
import { recoveryKey, stageMutation } from "./reviewRecovery";
function storage() {
  const values = new Map<string, string>();
  return { get length() { return values.size; }, key: (index: number) => [...values.keys()][index] ?? null,
    getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); } };
}
const value = { title: "Alpha study", paperId: "paper", datasetId: "data", taskText: "{}", note: "" };
function save(store: ReturnType<typeof storage>, scope = "blank", workspace = "w", changes = {}) {
  const key = draftKey(workspace, `create:${scope}`);
  store.setItem(key, JSON.stringify({ version: 1, scope: key, savedAt: "2026-10-09T10:00:00Z", value: { ...value, ...changes } }));
  return key;
}
describe("creation draft discovery and ownership", () => {
  it("discovers blank and copied creation scopes without leaking revisions or other workspaces", () => {
    const store = storage(); save(store); save(store, "study:revision", "w", { creationStage: "candidates" }); save(store, "blank", "other");
    store.setItem(draftKey("w", "revision:study:rev"), "broken unrelated revision");
    const result = discoverCreationDrafts(store, "w");
    expect(result.entries.map(entry => entry.scope)).toEqual(["blank", "study:revision"]);
    expect(result.errors).toEqual([]);
    expect(creationStage(result.entries[0].draft.value)).toBe("materials");
    expect(creationStage(result.entries[1].draft.value)).toBe("candidates");
    expect(() => creationScope("other", result.entries[0].key)).toThrow();
  });
  it("keeps malformed creation drafts untouched and accepts optional backwards-compatible fields", () => {
    const store = storage(); const key = save(store, "broken", "w", { creationStage: "approved" });
    const before = store.getItem(key); const result = discoverCreationDrafts(store, "w");
    expect(result.entries).toHaveLength(0); expect(result.errors).toHaveLength(1); expect(store.getItem(key)).toBe(before);
    expect(isCreationDraft(value)).toBe(true); expect(isCreationDraft({ ...value, creationMode: "automatic" })).toBe(false);
  });
  it("pins a pending creation request even when its draft has already been removed", () => {
    const store = storage(); const key = save(store);
    stageMutation(store, recoveryKey("w", "research-request"), "/researches", { title: "frozen" }, "original", { draft_key: key });
    const entry = discoverCreationDrafts(store, "w").entries[0];
    expect(() => discardCreationDraft(store, "w", entry)).toThrow(/待确认/);
    store.removeItem(key);
    const result = discoverCreationDrafts(store, "w");
    expect(result.pendingScope).toBe("blank"); expect(result.pending?.body.idempotency_key).toBe("original");
    expect(result.entries).toEqual([]);
  });
  it("does not erase a newer tab edit or a newly staged mutation after list rendering", () => {
    const store = storage(); const key = save(store); const entry = discoverCreationDrafts(store, "w").entries[0];
    save(store, "blank", "w", { title: "Newer tab" });
    expect(() => discardCreationDraft(store, "w", entry)).toThrow(/另一窗口/);
    const fresh = discoverCreationDrafts(store, "w").entries[0];
    stageMutation(store, recoveryKey("w", "research-request"), "/researches", { title: "frozen" }, "original", { draft_key: key });
    expect(() => discardCreationDraft(store, "w", fresh)).toThrow(/待确认/);
  });
  it("preserves malformed receipt bytes and blocks discarding when creation outcome cannot be checked", () => {
    const store = storage(); save(store); store.setItem(recoveryKey("w", "research-request"), "{broken");
    const result = discoverCreationDrafts(store, "w");
    expect(result.recoveryBlocked).toBe(true);
    expect(() => discardCreationDraft(store, "w", result.entries[0])).toThrow();
    expect(store.getItem(recoveryKey("w", "research-request"))).toBe("{broken");
  });
  it("discards only the explicitly selected unsubmitted draft without touching other scopes", () => {
    const store = storage(); const key = save(store); const other = save(store, "copied:rev");
    discardCreationDraft(store, "w", discoverCreationDrafts(store, "w").entries.find(entry => entry.key === key)!);
    expect(store.getItem(key)).toBeNull(); expect(store.getItem(other)).not.toBeNull();
  });
});
