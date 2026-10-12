import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, ApiTimeoutError, DEFAULT_READ_TIMEOUT_MS, MAX_READ_TIMEOUT_MS } from "./api";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function bodyResponse(body: Promise<unknown>): Response {
  return { ok: true, status: 200, json: vi.fn(() => body) } as unknown as Response;
}
function okResponse() { return new Response("[]", { status: 200 }); }

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  expect(vi.getTimerCount(), "Each settled request must release its deadline timer").toBe(0);
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("bounded read deadlines", () => {
  it.each([undefined, "GET", "HEAD", "get"])("terminates a stalled %s transport once without retrying", async (method) => {
    const fetch = vi.fn((_url: string, _options: RequestInit) => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetch);
    const pending = api("/papers", method ? { method } : {});
    const observed = expect(pending).rejects.toMatchObject({
      name: "ApiTimeoutError", code: "READ_TIMEOUT", status: 0,
      contractViolation: false, timeoutMs: DEFAULT_READ_TIMEOUT_MS,
    });
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS - 1);
    expect(fetch).toHaveBeenCalledTimes(1);
    const signal = fetch.mock.calls[0][1].signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await observed;
    expect(signal.aborted).toBe(true);
    expect(signal.reason).toBeInstanceOf(ApiTimeoutError);
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS * 2);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("uses one deadline across headers and a stalled body, then rejects late payloads", async () => {
    const headers = deferred<Response>();
    const body = deferred<unknown>();
    const response = bodyResponse(body.promise);
    vi.stubGlobal("fetch", vi.fn(() => headers.promise));
    const accepted = vi.fn();
    const pending = api("/papers", { readTimeoutMs: 100 }).then(accepted);
    const observed = expect(pending).rejects.toBeInstanceOf(ApiTimeoutError);
    await vi.advanceTimersByTimeAsync(75);
    headers.resolve(response);
    await vi.advanceTimersByTimeAsync(0);
    expect(response.json).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(25);
    await observed;
    body.resolve([]);
    await vi.advanceTimersByTimeAsync(0);
    expect(accepted).not.toHaveBeenCalled();
  });

  it("rejects a decoded payload that exceeds budget before a delayed timer can run", async () => {
    vi.spyOn(performance, "now").mockReturnValueOnce(10).mockReturnValue(111);
    const fetch = vi.fn().mockResolvedValue(okResponse());
    vi.stubGlobal("fetch", fetch);
    await expect(api("/papers", { readTimeoutMs: 100 })).rejects.toMatchObject({ code: "READ_TIMEOUT" });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  });

  it("does not parse a late response that arrives after the deadline", async () => {
    const headers = deferred<Response>();
    const json = vi.fn().mockResolvedValue([]);
    vi.stubGlobal("fetch", vi.fn(() => headers.promise));
    const pending = api("/papers", { readTimeoutMs: 5 });
    const observed = expect(pending).rejects.toMatchObject({ code: "READ_TIMEOUT" });
    await vi.advanceTimersByTimeAsync(5);
    await observed;
    headers.resolve({ ok: true, status: 200, json } as unknown as Response);
    await vi.advanceTimersByTimeAsync(0);
    expect(json).not.toHaveBeenCalled();
  });

  it("accepts a finite slower-read allowance and strips it from fetch options", async () => {
    const result = deferred<Response>();
    const fetch = vi.fn((_url: string, _options: RequestInit) => result.promise);
    vi.stubGlobal("fetch", fetch);
    const pending = api("/papers", { readTimeoutMs: MAX_READ_TIMEOUT_MS });
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS + 1);
    expect(fetch.mock.calls[0][1]).not.toHaveProperty("readTimeoutMs");
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(false);
    result.resolve(okResponse());
    await expect(pending).resolves.toEqual([]);
  });

  it.each([0, -1, 1.5, NaN, Infinity, MAX_READ_TIMEOUT_MS + 1])("refuses an unbounded or invalid allowance %s before dispatch", async (readTimeoutMs) => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    await expect(api("/papers", { readTimeoutMs })).rejects.toBeInstanceOf(RangeError);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("caller cancellation and cleanup", () => {
  it("keeps a caller's AbortError before headers and removes the listener", async () => {
    const caller = new AbortController();
    const add = vi.spyOn(caller.signal, "addEventListener");
    const remove = vi.spyOn(caller.signal, "removeEventListener");
    const fetch = vi.fn((_url: string, _options: RequestInit) => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetch);
    const pending = api("/papers", { signal: caller.signal });
    const reason = new DOMException("User switched research", "AbortError");
    const observed = expect(pending).rejects.toBe(reason);
    await vi.advanceTimersByTimeAsync(0);
    caller.abort(reason);
    await observed;
    expect(add).toHaveBeenCalledTimes(1);
    expect(remove).toHaveBeenCalledWith("abort", add.mock.calls[0][1]);
    expect((fetch.mock.calls[0][1].signal as AbortSignal).aborted).toBe(true);
    expect(caller.signal.reason).toBe(reason);
  });

  it("cleans the caller listener after timeout and a later caller abort cannot replace it", async () => {
    const caller = new AbortController();
    const add = vi.spyOn(caller.signal, "addEventListener");
    const remove = vi.spyOn(caller.signal, "removeEventListener");
    vi.stubGlobal("fetch", vi.fn((_url: string, _options: RequestInit) => new Promise<Response>(() => {})));
    const pending = api("/papers", { signal: caller.signal, readTimeoutMs: 5 });
    const observed = expect(pending).rejects.toBeInstanceOf(ApiTimeoutError);
    await vi.advanceTimersByTimeAsync(5);
    await observed;
    expect(remove).toHaveBeenCalledWith("abort", add.mock.calls[0][1]);
    expect(caller.signal.aborted).toBe(false);
    caller.abort();
    await expect(pending).rejects.toMatchObject({ code: "READ_TIMEOUT" });
  });

  it("does not dispatch for an already-aborted signal, even with a custom reason", async () => {
    const caller = new AbortController();
    caller.abort("navigated away");
    const add = vi.spyOn(caller.signal, "addEventListener");
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    await expect(api("/papers", { signal: caller.signal })).rejects.toMatchObject({ name: "AbortError" });
    expect(fetch).not.toHaveBeenCalled();
    expect(add).not.toHaveBeenCalled();
  });

  it("preserves AbortError during body parsing and never accepts its late data", async () => {
    const caller = new AbortController();
    const body = deferred<unknown>();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(bodyResponse(body.promise)));
    const accepted = vi.fn();
    const pending = api("/papers", { signal: caller.signal }).then(accepted);
    const observed = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(0);
    caller.abort();
    await observed;
    body.resolve([]);
    await vi.advanceTimersByTimeAsync(0);
    expect(accepted).not.toHaveBeenCalled();
  });

  it("allows a fresh request after a timed-out read without accepting stale late data", async () => {
    const body = deferred<unknown>();
    const fetch = vi.fn().mockResolvedValueOnce(bodyResponse(body.promise)).mockResolvedValueOnce(okResponse());
    vi.stubGlobal("fetch", fetch);
    const oldAccepted = vi.fn();
    const old = api("/papers", { readTimeoutMs: 2 }).then(oldAccepted);
    const observed = expect(old).rejects.toMatchObject({ code: "READ_TIMEOUT" });
    await vi.advanceTimersByTimeAsync(2);
    await observed;
    await expect(api("/papers")).resolves.toEqual([]);
    body.resolve([{ malformed: "stale value" }]);
    await vi.advanceTimersByTimeAsync(0);
    expect(oldAccepted).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("releases the caller listener and timer on success without aborting the caller", async () => {
    const caller = new AbortController();
    const add = vi.spyOn(caller.signal, "addEventListener");
    const remove = vi.spyOn(caller.signal, "removeEventListener");
    const fetch = vi.fn().mockResolvedValue(okResponse());
    vi.stubGlobal("fetch", fetch);
    await expect(api("/papers", { signal: caller.signal })).resolves.toEqual([]);
    expect(remove).toHaveBeenCalledWith("abort", add.mock.calls[0][1]);
    expect(vi.getTimerCount()).toBe(0);
    expect(caller.signal.aborted).toBe(false);
    const transportSignal = fetch.mock.calls[0][1].signal as AbortSignal;
    await vi.advanceTimersByTimeAsync(DEFAULT_READ_TIMEOUT_MS * 2);
    caller.abort();
    expect(transportSignal.aborted).toBe(false);
  });

  it("keeps transport failure distinct from a timeout and releases resources", async () => {
    const caller = new AbortController();
    const remove = vi.spyOn(caller.signal, "removeEventListener");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(api("/papers", { signal: caller.signal })).rejects.toMatchObject({
      code: "NETWORK_ERROR", status: 0, contractViolation: false,
    });
    expect(remove).toHaveBeenCalledTimes(1);
  });

  it("cleans deadlines on contract failures as well as malformed JSON", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response("<html>offline</html>")).mockResolvedValueOnce(new Response('{"unexpected": true}'));
    vi.stubGlobal("fetch", fetch);
    await expect(api("/papers")).rejects.toMatchObject({ contractViolation: true, code: "API_ERROR" });
    await expect(api("/papers")).rejects.toBeInstanceOf(ApiError);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe("writes keep the existing recovery contract", () => {
  it.each(["POST", "PUT", "PATCH", "DELETE"])("does not deadline, retry, or replace the signal of %s", async (method) => {
    const caller = new AbortController();
    const response = deferred<Response>();
    const fetch = vi.fn((_url: string, _options: RequestInit) => response.promise);
    vi.stubGlobal("fetch", fetch);
    const pending = api("/papers", { method, signal: caller.signal, readTimeoutMs: 1 });
    const observed = expect(pending).rejects.toMatchObject({ status: 503 });
    await vi.advanceTimersByTimeAsync(MAX_READ_TIMEOUT_MS * 2);
    expect(vi.getTimerCount()).toBe(0);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1].signal).toBe(caller.signal);
    expect(fetch.mock.calls[0][1]).not.toHaveProperty("readTimeoutMs");
    response.resolve(new Response(JSON.stringify({ detail: "Temporarily unavailable" }), { status: 503 }));
    await observed;
  });

  it("keeps a caller-cancelled write ambiguous instead of creating a timeout or rejection response", async () => {
    const caller = new AbortController();
    const body = deferred<unknown>();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(bodyResponse(body.promise)));
    const pending = api("/papers", { method: "POST", signal: caller.signal });
    const observed = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(0);
    caller.abort();
    body.reject(caller.signal.reason);
    await observed;
  });
});
