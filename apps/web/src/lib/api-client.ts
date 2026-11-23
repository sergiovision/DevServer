/**
 * Browser-side JSON client for this app's own `/api/*` routes.
 *
 * Why it exists: components used to hand-roll `fetch` + `res.ok` + `res.json()`
 * dozens of times, each with a slightly different idea of what a failure looks
 * like. Every route answers errors as `{error, detail?}` (see `route-utils.ts`
 * and `api-errors.ts`), so one client can decode that shape once and throw a
 * typed `ApiError` that callers can `catch` and show.
 *
 * Invariants:
 * - A non-2xx response always throws `ApiError` with the HTTP status.
 * - A network failure (fetch itself rejected) throws `ApiError` with status 0,
 *   so callers never need a second `catch` shape.
 * - An empty or non-JSON 2xx body resolves to `null` rather than throwing.
 *
 * The global fetch interceptor in `NotificationProvider` still sees every call
 * (this module calls `fetch`), so DB-down diagnostics keep working unchanged.
 */

/** A failed `/api/*` call, carrying what the route said about it. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, message: string, detail: unknown = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }

  /** True when the request never got an HTTP answer (offline, DNS, refused). */
  get isNetworkError(): boolean {
    return this.status === 0;
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}

/** A readable message for any thrown value — for toasts and inline alerts. */
export function errorMessage(err: unknown, fallback = 'Request failed'): string {
  if (err instanceof Error && err.message) return err.message;
  if (typeof err === 'string' && err) return err;
  return fallback;
}

/**
 * Pick the human message out of an error body. Routes answer `{error}`;
 * worker relays sometimes still carry FastAPI's `{detail}`.
 */
export function messageFromErrorBody(body: unknown, status: number): string {
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>;
    for (const key of ['error', 'detail', 'message']) {
      const value = record[key];
      if (typeof value === 'string' && value) return value;
    }
  }
  if (typeof body === 'string' && body.trim()) return body.trim().slice(0, 300);
  return `Request failed (${status})`;
}

export interface RequestOptions {
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Forwarded to `fetch`; pass `'no-store'` for reads that must never be served from the HTTP cache. */
  cache?: RequestCache;
}

type JsonBody = unknown;

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text().catch(() => '');
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function request<T>(
  method: string,
  url: string,
  body: JsonBody | undefined,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { ...(options.headers ?? {}) };
  const init: RequestInit = { method, headers, signal: options.signal };
  if (options.cache) init.cache = options.cache;
  if (body !== undefined) {
    headers['Content-Type'] = headers['Content-Type'] ?? 'application/json';
    init.body = JSON.stringify(body);
  }

  let res: Response;
  try {
    res = await fetch(url, init);
  } catch (err) {
    // An abort is the caller's own decision — let it surface untouched so
    // `useResource` can recognise and ignore it.
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError(0, 'Network error — server unreachable', err instanceof Error ? err.message : null);
  }

  const parsed = await readBody(res);
  if (!res.ok) {
    const detail =
      parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>).detail ?? parsed : parsed;
    throw new ApiError(res.status, messageFromErrorBody(parsed, res.status), detail ?? null);
  }
  return (typeof parsed === 'string' ? null : parsed) as T;
}

export const api = {
  get: <T = unknown>(url: string, options?: RequestOptions) => request<T>('GET', url, undefined, options),
  post: <T = unknown>(url: string, body?: JsonBody, options?: RequestOptions) =>
    request<T>('POST', url, body ?? {}, options),
  put: <T = unknown>(url: string, body?: JsonBody, options?: RequestOptions) =>
    request<T>('PUT', url, body ?? {}, options),
  patch: <T = unknown>(url: string, body?: JsonBody, options?: RequestOptions) =>
    request<T>('PATCH', url, body ?? {}, options),
  delete: <T = unknown>(url: string, options?: RequestOptions) => request<T>('DELETE', url, undefined, options),
};
