import { describeError } from "./domain";
import { assertApiResponse } from "./generated/api-contract";

export class ApiError extends Error {
  status: number;
  contractViolation: boolean;
  constructor(message: string, status: number, contractViolation = false) {
    super(message);
    this.status = status;
    this.contractViolation = contractViolation;
  }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
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
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError("无法连接研究服务。请确认 API 已启动，然后重试。", 0);
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
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
export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });
