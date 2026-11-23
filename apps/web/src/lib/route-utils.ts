/**
 * Shared plumbing for `src/app/api/**` route handlers.
 *
 * Why it exists: route handlers each parsed `[id]` with `parseInt` (so
 * `"12abc"` became 12 and `"abc"` reached SQL as NaN → 500), and each proxied
 * the worker with its own `fetch`, error shape and "worker down" handling.
 * This module is the one place for those rules:
 *
 * - `parseRouteId` — a positive integer or null; callers answer `invalidId()`
 *   (400), never a 500.
 * - `jsonError` — the `{error, detail?}` body every route answers with.
 * - `callWorker` — reach the worker; a thrown fetch (refused, DNS, timeout)
 *   becomes `null` so the caller decides the fallback.
 * - `workerError` — a failed worker response as `{error}` with its status,
 *   for routes that do more than relay.
 * - `proxyWorker` — relay a worker call verbatim: the worker's own error
 *   `detail` is forwarded with its status; unreachable → 502
 *   "Worker unreachable".
 *
 * `worker-client.ts` keeps the lower-level helpers it already exported; this
 * module re-exports what routes need so a route imports from one place.
 */

import { NextResponse } from 'next/server';
import { callWorker, readWorkerJson } from './worker-client';

export { callWorker, readWorkerJson };

/** Next.js 16 passes dynamic segments as a promise. */
export interface RouteContext<P extends Record<string, string> = { id: string }> {
  params: Promise<P>;
}

/**
 * Parse a numeric `[id]` route segment.
 * Returns null unless it is a positive integer, so `"12abc"` or `"-1"` are
 * rejected with a 400 instead of reaching SQL as NaN.
 */
export function parseRouteId(raw: string | undefined | null): number | null {
  if (raw == null || raw.trim() === '') return null;
  const id = Number(raw);
  return Number.isInteger(id) && id > 0 ? id : null;
}

/** The app-wide error body: `{error, detail?}` with the given status. */
export function jsonError(status: number, error: string, detail?: unknown): NextResponse {
  const body: Record<string, unknown> = { error };
  if (detail !== undefined && detail !== null) body.detail = detail;
  return NextResponse.json(body, { status });
}

/** 400 for an `[id]` segment that is not a positive integer. */
export function invalidId(name = 'id'): NextResponse {
  return jsonError(400, `Invalid ${name}`);
}

export const WORKER_UNREACHABLE_MESSAGE = 'Worker unreachable';

/** 502 for "the worker did not answer at all". */
export function workerUnreachable(): NextResponse {
  return jsonError(502, WORKER_UNREACHABLE_MESSAGE);
}

/**
 * Pull the human message out of a worker error body. FastAPI answers
 * `{detail: "..."}` (or `{detail: [validation errors]}`); some routes answer
 * `{error}`.
 */
export function workerErrorMessage(body: Record<string, unknown>, status: number): string {
  const detail = body.detail ?? body.error;
  if (typeof detail === 'string' && detail) return detail;
  if (detail !== undefined && detail !== null) return JSON.stringify(detail);
  return `Worker returned ${status}`;
}

/**
 * Answer a failed worker response: the worker's own message with the
 * worker's status. For routes that call the worker and then do more work, so
 * cannot hand the whole call to `proxyWorker`.
 */
export function workerError(res: Response, body: Record<string, unknown>): NextResponse {
  return jsonError(res.status, workerErrorMessage(body, res.status));
}

export interface ProxyOptions {
  method?: string;
  /** JSON body to send; omitted for GET/DELETE. */
  body?: unknown;
  timeoutMs?: number;
  /** Transform a successful worker body before answering. */
  transform?: (body: Record<string, unknown>) => unknown;
  /**
   * Status to answer a success with, when this route's contract differs from
   * the worker's (e.g. 201 for a create the worker answers with 200).
   */
  successStatus?: number;
}

/**
 * Relay a worker endpoint as this route's answer.
 *
 * - 2xx → the worker's JSON (optionally transformed), same status unless
 *   `successStatus` overrides it.
 * - non-2xx → `{error: <worker detail>}` with the worker's status, so the
 *   browser sees *why* (e.g. "task not found"), not a generic 500.
 * - unreachable → 502 "Worker unreachable".
 */
export async function proxyWorker(path: string, options: ProxyOptions = {}): Promise<NextResponse> {
  const { method = 'GET', body, timeoutMs, transform, successStatus } = options;
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const res = await callWorker(path, init, timeoutMs);
  if (!res) return workerUnreachable();
  const json = await readWorkerJson(res);
  if (!res.ok) return workerError(res, json);
  return NextResponse.json(transform ? transform(json) : json, {
    status: successStatus ?? res.status,
  });
}
