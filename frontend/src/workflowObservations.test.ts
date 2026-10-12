import { describe, expect, it } from "vitest";
import { clearSubmittedObservationDraft, loadObservationDraft, MAX_OBSERVATION_BYTES, observationPath, observationScopeKey, ObservationRequestEpoch, observedSeconds, parseObservation, saveObservationDraft } from "./workflowObservations";

function storage() {
  const values = new Map<string, string>();
  return { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
}

describe("workflow observation import boundaries", () => {
  it("requires an explicit source matching the untouched input", () => {
    const raw = JSON.stringify({record_status:"observed", source:"automation", observed_active_seconds:null});
    expect(() => parseObservation(raw, "")).toThrow(/明确声明/);
    expect(() => parseObservation(raw, "human")).toThrow(/不一致/);
    expect(parseObservation(raw, "automation")).toEqual(JSON.parse(raw));
    // Unknown fields remain for strict server rejection; the browser never strips them.
    expect(parseObservation(raw, "automation")).toHaveProperty("observed_active_seconds", null);
  });
  it("rejects blank, pending, non-object, malformed and oversized documents", () => {
    for (const raw of ["", "[]", "null", "{", '{"source":"human","record_status":"pending_human_observation"}']) {
      expect(() => parseObservation(raw, "human")).toThrow();
    }
    expect(() => parseObservation(JSON.stringify({source:"human", record_status:"observed", note:"汉".repeat(MAX_OBSERVATION_BYTES)}), "human")).toThrow(/256 KiB/);
  });
  it("rejects duplicate keys at every nesting level including escaped aliases", () => {
    for (const raw of [
      '{"source":"human","source":"automation","record_status":"observed"}',
      '{"source":"automation","record_status":"observed","nested":[{"name":1,"na\\u006de":2}]}',
      '{"source":"automation","record_status":"observed","nested":{"name":"value\\\"with quote","name":false}}',
    ]) expect(() => parseObservation(raw, "automation")).toThrow(/重复字段/);
    expect(parseObservation('{"source":"automation","record_status":"observed","a":{"name":true},"b":{"name":false},"array":[null,1,-1.2e-3,"a:b"]}', "automation")).toHaveProperty("array");
    expect(() => parseObservation('{"source":"automation","record_status":"observed","number":1e400}', "automation")).toThrow(/数字/);
    expect(() => parseObservation('['.repeat(66) + '0' + ']'.repeat(66), "automation")).toThrow(/64/);
  });
  it("isolates workspace and research draft scopes and safely encodes identifiers", () => {
    const key = observationScopeKey("workspace", "research", "draft");
    expect(key).not.toBe(observationScopeKey("other", "research", "draft"));
    expect(key).not.toBe(observationScopeKey("workspace", "other", "draft"));
    expect(key).not.toBe(observationScopeKey("workspace", "research", "import"));
    expect(observationPath("a/b?c")).toBe("/researches/a%2Fb%3Fc/workflow-observations");
    expect(() => observationPath("")).toThrow();
  });
  it("restores drafts without inventing source and clears only the submitted draft", () => {
    const s = storage(), key = observationScopeKey("w", "r", "draft"), first = {raw:"first", source:"" as const};
    saveObservationDraft(s, key, first);
    expect(loadObservationDraft(s, key)).toEqual(first);
    const later = {raw:"new edits in another tab", source:"automation" as const};
    saveObservationDraft(s, key, later);
    clearSubmittedObservationDraft(s, key, first);
    expect(loadObservationDraft(s, key)).toEqual(later);
    clearSubmittedObservationDraft(s, key, later);
    expect(loadObservationDraft(s, key)).toBeNull();
  });
  it("detects malformed stored drafts and failed durability", () => {
    const s = storage();
    s.setItem("key", JSON.stringify({version:1,scope:"different",draft:{raw:"text",source:"human"}}));
    expect(() => loadObservationDraft(s, "key")).toThrow(/作用域/);
    s.setItem("key", JSON.stringify({version:1,scope:"key",draft:{raw:"text",source:"unknown"}}));
    expect(() => loadObservationDraft(s, "key")).toThrow();
    expect(() => saveObservationDraft({getItem:()=>null,setItem:()=>{}}, "key", {raw:"text",source:"human"})).toThrow(/保存/);
  });
  it("invalidates slow responses after edits or A to B to A selection changes", () => {
    const scope = new ObservationRequestEpoch();
    const first = scope.begin(); scope.invalidate(); const second = scope.begin();
    expect(scope.isCurrent(first)).toBe(false); expect(scope.isCurrent(second)).toBe(true);
  });
  it("shows missing timing as unmeasured, never zero or a derived value", () => {
    expect(observedSeconds(null)).toBe("未测量"); expect(observedSeconds(undefined)).toBe("未测量");
    expect(observedSeconds(NaN)).toBe("未测量"); expect(observedSeconds(0)).toBe("0.00 秒");
    expect(observedSeconds(61.125)).toBe("61.13 秒");
  });
});
