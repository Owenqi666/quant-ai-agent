import { describeError } from "./domain";
import { assertApiResponse } from "./generated/api-contract";

export type ApiErrorCode = "API_ERROR" | "NETWORK_ERROR" | "READ_TIMEOUT";
export const DEFAULT_READ_TIMEOUT_MS = 15_000;
export const MAX_READ_TIMEOUT_MS = 60_000;

export interface ApiRequestOptions extends RequestInit {
  /** GET/HEAD only. An explicit finite allowance for slower complete reads. */
  readTimeoutMs?: number;
}

export class ApiError extends Error {
  status: number;
  contractViolation: boolean;
  code: ApiErrorCode;
  constructor(message: string, status: number, contractViolation = false, code: ApiErrorCode = "API_ERROR") {
    super(message);
    this.status = status;
    this.contractViolation = contractViolation;
    this.code = code;
  }
}

export class ApiTimeoutError extends ApiError {
  timeoutMs: number;
  constructor(timeoutMs: number) {
    super(`读取研究服务超过 ${timeoutMs / 1000} 秒。请检查连接后重试读取；本次不会自动重试。`, 0, false, "READ_TIMEOUT");
    this.name = "ApiTimeoutError";
    this.timeoutMs = timeoutMs;
  }
}

function callerAbortError(signal: AbortSignal): DOMException {
  return signal.reason instanceof DOMException && signal.reason.name === "AbortError"
    ? signal.reason
    : new DOMException("请求已取消。", "AbortError");
}

function checkAbort(signal?: AbortSignal | null): void {
  if (signal?.aborted) {
    // Only the private GET/HEAD controller receives an ApiTimeoutError reason.
    if (signal.reason instanceof ApiTimeoutError) throw signal.reason;
    throw callerAbortError(signal);
  }
}

async function withReadDeadline<T>(
  caller: AbortSignal | null | undefined,
  timeoutMs: number,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > MAX_READ_TIMEOUT_MS) {
    throw new RangeError(`readTimeoutMs 必须是 1 到 ${MAX_READ_TIMEOUT_MS} 之间的有限整数。`);
  }
  if (caller?.aborted) throw callerAbortError(caller);
  const controller = new AbortController();
  const startedAt = performance.now();
  let settled = false;
  let rejectInterrupted!: (reason: Error) => void;
  const interrupted = new Promise<never>((_, reject) => { rejectInterrupted = reject; });
  const interrupt = (reason: Error) => {
    if (settled || controller.signal.aborted) return;
    // Settle the deadline/caller race even when a fetch mock or transport does
    // not honour AbortSignal. Aborting still stops the real fetch and body read.
    rejectInterrupted(reason);
    controller.abort(reason);
  };
  const onCallerAbort = () => interrupt(callerAbortError(caller!));
  caller?.addEventListener("abort", onCallerAbort, { once: true });
  const timer = setTimeout(() => interrupt(new ApiTimeoutError(timeoutMs)), timeoutMs);
  try {
    // Keep fetch dispatch synchronous with the existing API helper. The race
    // bounds both transport stages, including transports that ignore abort.
    const result = await Promise.race([interrupted, operation(controller.signal)]);
    // JSON decoding may occupy the event loop and delay the timer callback.
    // Never accept a successful payload whose complete read exceeded budget.
    if (performance.now() - startedAt >= timeoutMs) {
      const error = new ApiTimeoutError(timeoutMs);
      interrupt(error);
      throw error;
    }
    checkAbort(controller.signal);
    return result;
  } finally {
    settled = true;
    clearTimeout(timer);
    caller?.removeEventListener("abort", onCallerAbort);
  }
}

async function request<T>(path: string, options: RequestInit): Promise<T> {
  checkAbort(options.signal);
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: {
        ...(options.body instanceof FormData
          ? {}
          : { "Content-Type": "application/json" }),
        ...options.headers,
      },
    });
    checkAbort(options.signal);
  } catch (error) {
    checkAbort(options.signal);
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("无法连接研究服务。请确认 API 已启动，然后重试。", 0, false, "NETWORK_ERROR");
  }
  let data: unknown;
  try {
    data = await response.json();
    checkAbort(options.signal);
  } catch (error) {
    checkAbort(options.signal);
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(
      "服务返回了无法读取的响应，请检查 API 日志后重试。",
      response.status,
      true,
    );
  }
  try {
    assertApiResponse(`/api${path}`, options.method || "GET", response.status, data);
  } catch (error) {
    throw new ApiError(`服务响应不符合接口契约，操作未被确认为成功。${describeError(error)}`, response.status, true);
  }
  if (!response.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data ? data.detail : data;
    const suffix =
      response.status === 409
        ? " 请刷新最新状态后重试；当前编辑内容已保留。"
        : "";
    throw new ApiError(
      `${describeError(detail) || `请求失败 (${response.status})`}${suffix}`,
      response.status,
    );
  }
  return data as T;
}

export async function api<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { readTimeoutMs = DEFAULT_READ_TIMEOUT_MS, ...requestOptions } = options;
  const method = (requestOptions.method || "GET").toUpperCase();
  if (method === "GET" || method === "HEAD") {
    return withReadDeadline(requestOptions.signal, readTimeoutMs,
      (signal) => request<T>(path, { ...requestOptions, signal }));
  }
  // Writes retain their original caller signal and recoverable-mutation
  // semantics: no default deadline, no automatic retry, no assumed rejection.
  return request<T>(path, requestOptions);
}

export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });
