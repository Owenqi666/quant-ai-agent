import { describe, expect, it } from "vitest";
import { EVENT_CACHE_LIMIT, EVENT_PAGES_PER_CYCLE, EventFeed } from "./events";
import type { EventPage, EventPageRequest, FetchEventPage, RunEvent } from "./events";

function rows(count: number, runId = "run", offset = 0): RunEvent[] {
  return Array.from({ length: count }, (_, index) => ({
    id: (offset + index + 1) * 3, sequence: (offset + index + 1) * 3,
    run_id: runId, attempt_id: index < count / 2 ? "first" : "second",
    payload: { engine_sequence: 1 },
  }));
}
function page(events: RunEvent[], runId: string, request: EventPageRequest): EventPage {
  const through = request.through ?? events.at(-1)?.sequence ?? 0;
  const snapshot = events.filter((event) => event.sequence <= through);
  const remaining = snapshot.filter((event) => event.sequence > request.after);
  const items = remaining.slice(0, request.limit);
  return { run_id: runId, items, next_cursor: items.at(-1)?.sequence ?? request.after,
    high_watermark: through, total_records: snapshot.length, has_more: remaining.length > items.length };
}
async function drain(feed: EventFeed) {
  for (let round = 0; round < 20; round += 1) {
    await feed.sync();
    if (feed.state.error) throw new Error(feed.state.error);
    if (feed.state.caughtUp) return;
  }
  throw new Error("Event feed did not finish its bounded cycles");
}

describe("durable event pagination", () => {
  it.each([0, 999, 1000, 1001, 2501])("consumes %i events with gap IDs and a bounded live cache", async (count) => {
    const events = rows(count);
    const seen: number[] = [];
    const feed = new EventFeed("run", async (id, request) => {
      const result = page(events, id, request);
      seen.push(...result.items.map((event) => event.sequence));
      return result;
    }, () => {});
    await drain(feed);
    expect(seen).toEqual(events.map((event) => event.sequence));
    expect(feed.state.receivedRecords).toBe(count);
    expect(feed.state.totalRecords).toBe(count);
    expect(feed.state.items).toHaveLength(Math.min(count, EVENT_CACHE_LIMIT));
    expect(feed.state.cursor).toBe(events.at(-1)?.sequence ?? 0);
  });

  it("bounds each catch-up cycle and fixes its watermark while new events arrive", async () => {
    let events = rows(1001);
    const originalWatermark = events.at(-1)!.sequence;
    const requests: EventPageRequest[] = [];
    const feed = new EventFeed("run", async (id, request) => {
      requests.push(request);
      const result = page(events, id, request);
      if (requests.length === 1) events = [...events, ...rows(4, "run", events.length)];
      return result;
    }, () => {});
    await feed.sync();
    expect(requests).toHaveLength(EVENT_PAGES_PER_CYCLE);
    expect(feed.state.caughtUp).toBe(false);
    await drain(feed);
    expect(feed.state.receivedRecords).toBe(1001);
    expect(requests.slice(1).every((request) => request.through === originalWatermark)).toBe(true);
    await feed.sync();
    expect(feed.state.receivedRecords).toBe(1005);
    expect(requests.at(-1)?.through).toBeUndefined();
  });

  it("retains merged events and retries a failed middle page from the same cursor", async () => {
    const events = rows(601);
    let fail = true;
    const after: number[] = [];
    const feed = new EventFeed("run", async (id, request) => {
      after.push(request.after);
      if (request.after === 600 && fail) { fail = false; throw new Error("network unavailable"); }
      return page(events, id, request);
    }, () => {});
    await feed.sync();
    expect(feed.state.error).toContain("network");
    expect(feed.state.cursor).toBe(600);
    expect(feed.state.items).toHaveLength(200);
    await drain(feed);
    expect(after.slice(0, 3)).toEqual([0, 600, 600]);
    expect(feed.state.receivedRecords).toBe(601);
    expect(new Set(feed.state.items.map((event) => event.id)).size).toBe(601);
  });

  it("uses database IDs across attempts even when engine sequences restart", async () => {
    const events = rows(4);
    const feed = new EventFeed("run", async (id, request) => page(events, id, request), () => {});
    await drain(feed);
    expect(feed.state.items.map((event) => event.attempt_id)).toEqual(["first", "first", "second", "second"]);
    expect(feed.state.receivedRecords).toBe(4);
    await feed.sync();
    expect(feed.state.receivedRecords).toBe(4);
    expect(feed.state.items).toHaveLength(4);
  });

  it("isolates an old run's late response even if the transport ignores abort", async () => {
    let resolve!: (page: EventPage) => void;
    let oldUpdates = 0;
    const a = new EventFeed("a", () => new Promise<EventPage>((done) => { resolve = done; }), () => { oldUpdates += 1; });
    const pending = a.sync();
    a.dispose();
    const updatesAtSwitch = oldUpdates;
    const b = new EventFeed("b", async (id, request) => page(rows(1, "b"), id, request), () => {});
    await b.sync();
    resolve(page(rows(1, "a"), "a", { after: 0, limit: 200, signal: new AbortController().signal }));
    await pending;
    expect(oldUpdates).toBe(updatesAtSwitch);
    expect(b.state.items[0].run_id).toBe("b");
  });

  it("requests a fresh watermark after a terminal observation raced an earlier snapshot", async () => {
    let events = rows(2);
    let resolve!: (page: EventPage) => void;
    let captured!: EventPage;
    let first = true;
    const feed = new EventFeed("run", async (id, request) => {
      if (!first) return page(events, id, request);
      first = false;
      captured = page(events, id, request);
      return new Promise<EventPage>((done) => { resolve = done; });
    }, () => {});
    const pending = feed.sync();
    events = [...events, ...rows(1, "run", 2)];
    feed.invalidateSnapshot(); // GET run now says completed; old events request is still pending.
    resolve(captured);
    await pending;
    expect(feed.state.caughtUp).toBe(false);
    await feed.sync();
    expect(feed.state.items).toHaveLength(3);
    expect(feed.state.caughtUp).toBe(true);
  });

  it("reads evicted history in bounded pages without changing the live cursor", async () => {
    const events = rows(2501);
    const feed = new EventFeed("run", async (id, request) => page(events, id, request), () => {});
    await drain(feed);
    const liveCursor = feed.state.cursor;
    expect(feed.state.items[0].sequence).toBe(events[1501].sequence);
    await feed.openHistory();
    const historyIds = feed.state.history!.items.map((event) => event.id);
    while (feed.state.history!.hasMore) {
      await feed.nextHistory();
      expect(feed.state.history!.items.length).toBeLessThanOrEqual(200);
      historyIds.push(...feed.state.history!.items.map((event) => event.id));
    }
    expect(historyIds).toEqual(events.map((event) => event.id));
    expect(feed.state.cursor).toBe(liveCursor);
    expect(feed.state.items).toHaveLength(EVENT_CACHE_LIMIT);
    feed.closeHistory();
    expect(feed.state.history).toBeNull();
  });

  it("retains a displayed history page on error and retries its next page", async () => {
    const events = rows(401);
    let historyFail = false;
    const feed = new EventFeed("run", async (id, request) => {
      if (historyFail && request.after === 600) { historyFail = false; throw new Error("history failure"); }
      return page(events, id, request);
    }, () => {});
    await drain(feed);
    await feed.openHistory();
    historyFail = true;
    await feed.nextHistory();
    expect(feed.state.history!.error).toBe("history failure");
    expect(feed.state.history!.items[0].id).toBe(3);
    await feed.retryHistory();
    expect(feed.state.history!.items[0].id).toBe(603);
    expect(feed.state.history!.loadedThrough).toBe(400);
  });

  it("preserves a failed cursor and rejects invalid cross-run or stalled page responses", async () => {
    const good: FetchEventPage = async (id, request) => page(rows(1), id, request);
    let bad = false;
    const feed = new EventFeed("run", async (id, request) => {
      if (bad) throw new Error("409: cursor exceeds current watermark; reset required");
      return good(id, request);
    }, () => {});
    await feed.sync();
    bad = true;
    await feed.sync();
    expect(feed.state.cursor).toBe(3);
    expect(feed.state.items).toHaveLength(1);
    expect(feed.state.error).toContain("409");
    for (const change of [ { run_id: "other" }, { items: [], next_cursor: 0, has_more: true } ]) {
      const invalid = new EventFeed("run", async (id, request) => ({ ...await good(id, request), ...change }), () => {});
      await invalid.sync();
      expect(invalid.state.error).not.toBe("");
      expect(invalid.state.cursor).toBe(0);
    }
  });
});
