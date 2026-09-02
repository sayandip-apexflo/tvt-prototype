export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const body: unknown = response.status === 204 ? null : contentType.includes("application/json")
    ? await response.json() : await response.text();
  if (!response.ok) {
    const document = body && typeof body === "object" ? body as Record<string, unknown> : null;
    const detail = document?.detail || document?.error || body;
    throw new ApiError(typeof detail === "string" ? detail : `Request failed (${response.status})`, response.status);
  }
  return body as T;
}

export const get = <T,>(path: string) => request<T>(path);
export const send = <T,>(path: string, method: string, body?: unknown) => request<T>(path, {
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
});
