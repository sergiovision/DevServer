/**
 * Small helpers for route handlers that relay a request to the worker.
 *
 * The browser never talks to the worker directly, so many API routes are thin
 * proxies. They all need the same three things: a call that reports "worker
 * unreachable" instead of throwing, a tolerant JSON read, and a way to turn a
 * worker error (FastAPI's `{detail}`) into this app's `{error}` shape.
 */

import { NextResponse } from 'next/server';
import { WORKER_URL } from './worker-url';

const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * Call a worker endpoint. Resolves to `null` when the worker cannot be reached
 * (connection refused, DNS failure, timeout) so callers can choose a fallback.
 */
export async function callWorker(
  path: string,
  init: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Response | null> {
  try {
    return await fetch(`${WORKER_URL}${path}`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(timeoutMs),
      ...init,
    });
  } catch {
    return null;
  }
}

/** Parse a worker response body as a JSON object; `{}` when it is not one. */
export async function readWorkerJson(res: Response): Promise<Record<string, unknown>> {
  const body: unknown = await res.json().catch(() => null);
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    return body as Record<string, unknown>;
  }
  return {};
}

/** Relay a failed worker response with its status and a readable message. */
export function workerErrorResponse(
  res: Response,
  body: Record<string, unknown>,
): NextResponse {
  const message = body.detail ?? body.error ?? `worker returned ${res.status}`;
  return NextResponse.json({ error: String(message) }, { status: res.status });
}

/** The response for "the worker did not answer at all". */
export function workerUnreachableResponse(): NextResponse {
  return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
}
