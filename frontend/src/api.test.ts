import { afterEach, describe, expect, it, vi } from "vitest";
import { assertApiResponse } from "./generated/api-contract";
import { api, ApiError, post } from "./api";

afterEach(() => vi.unstubAllGlobals());
describe("API failures are explicit and never reported as success", () => {
  it("preserves a 409 conflict and tells the user to retain their editor input", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Stale revision" }), {
          status: 409,
        }),
      ),
    );
    await expect(post("/researches/r/revisions", {})).rejects.toMatchObject({
      status: 409,
      message: expect.stringContaining("编辑内容已保留"),
    });
  });
  it("reports an unavailable backend clearly", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );
    await expect(api("/health")).rejects.toMatchObject({
      status: 0,
      message: expect.stringContaining("API 已启动"),
    });
  });
  it("rejects non-JSON replies instead of treating an HTML proxy fallback as data", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response("<html>bad gateway</html>", { status: 502 }),
        ),
    );
    await expect(api("/health")).rejects.toBeInstanceOf(ApiError);
  });
  it("does not override multipart upload boundaries", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ id: "p", title: "paper", sha256: "digest", created_at: "now", pages: [{ page: 1, text: "original" }], extraction: "pypdf" }), { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    await api("/papers", { method: "POST", body: new FormData() });
    expect(fetch.mock.calls[0][1].headers).toEqual({});
  });
});


describe("generated API response contracts", () => {
  it("validates domain discriminators and rejects mixed source permissions", () => {
    const value={id:"domain_job_"+"a".repeat(64),digest:"a".repeat(64),created_at:"2020-01-01T00:00:00Z",source_kind:"monthly_fixture",source_id:"protocol_"+"b".repeat(64),source_digest:"b".repeat(64),budget:{max_steps:12,max_failures:2,max_seconds:180},usage:{steps:0,failures:0,elapsed_seconds:0},state:"created",stop_reason:null,experiment_id:null,attempt_id:null,provider_connected:false,semantic_fidelity:"unverified",source:{kind:"monthly_fixture",protocol_id:"protocol_"+"b".repeat(64),protocol_digest:"b".repeat(64),config:{schema_version:1,start_month:"2025-01",end_month:"2025-02",cost_bps:0,min_assets:18}},note:"Fixture",context_json:"{}",steps:[],result_json:null};
    expect(()=>assertApiResponse("/api/domain-research-jobs/example","GET",200,value)).not.toThrow();
    expect(()=>assertApiResponse("/api/domain-research-jobs/example","GET",200,{...value,source:{...value.source,kind:"arbitrary_network"}})).toThrow();
    expect(()=>assertApiResponse("/api/domain-research-jobs/example","GET",200,{...value,source:{...value.source,study_id:"author_study_"+"c".repeat(64)}})).toThrow();
  });
  const run = { id: "r", research_id: "research", revision_id: "v1", status: "queued", mode: "fixed", created_at: "now", started_at: null, finished_at: null, error: null, attempt_count: 0 };
  it("accepts legitimate null fields and rejects missing fields or unknown states", () => {
    expect(() => assertApiResponse("/api/runs", "GET", 200, [run])).not.toThrow();
    const { revision_id: _revision, ...missing } = run;
    expect(() => assertApiResponse("/api/runs", "GET", 200, [missing])).toThrow();
    expect(() => assertApiResponse("/api/runs", "GET", 200, [{ ...run, status: "mystery" }])).toThrow();
  });
  it("rejects malformed error payloads rather than stringify arbitrary objects", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { surprise: true } }), { status: 409 })));
    await expect(api("/runs")).rejects.toMatchObject({ message: expect.stringContaining("接口契约"), status: 409 });
  });
  it("rejects a missing nested hypothesis attribution", () => {
    const revision = { id: "v1", research_id: "r", number: 1, note: "manual", digest: "digest", created_at: "now", task: {
      evidence: [{ id: "e", page: 1, quote: "formula" }], hypotheses: [{ id: "h", claim: "claim", evidence_ids: ["e"], economic_mechanism: "unconfirmed", mechanism_attribution: "user_modification", signal_direction: "local long", required_fields: ["open"], assumptions: [] }],
      candidates: [], evaluation: {}, budget: {},
    } };
    expect(() => assertApiResponse("/api/researches/r/revisions", "POST", 201, revision)).toThrow();
  });
  it("turns malformed successful HTTP responses into explicit failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify([{ ...run, status: "invented" }]))));
    await expect(api("/runs")).rejects.toMatchObject({ status: 200, message: expect.stringContaining("未被确认为成功") });
  });
});
