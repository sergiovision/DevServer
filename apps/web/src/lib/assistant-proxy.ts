import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

/**
 * Shared plumbing for the Ask Agent panel's streaming proxy routes.
 *
 * The browser cannot reach the worker directly — it has no CORS and must never
 * be exposed — so every assistant turn is relayed through Next.js. The relay is
 * a byte-for-byte passthrough of an SSE body, which has three easy-to-miss
 * requirements, all handled here:
 *
 * 1. `signal: request.signal` must be forwarded. Without it, closing the
 *    browser tab leaves the worker streaming into a dead socket and, in agent
 *    mode, an orphaned CLI subprocess holding a repo directory.
 * 2. The outer Response gets **fresh** headers. Re-using the upstream headers
 *    drags `content-length` / `content-encoding` along and truncates or
 *    corrupts the stream.
 * 3. Errors have to come back as JSON *before* the stream starts. Once bytes
 *    are flowing the status is already sent, so an in-stream failure surfaces
 *    as an `{"type":"error"}` event instead — which is exactly what the worker
 *    emits.
 */

export const SSE_HEADERS = {
  'Content-Type': 'text/event-stream; charset=utf-8',
  'Cache-Control': 'no-cache, no-transform',
  Connection: 'keep-alive',
  'X-Accel-Buffering': 'no',
} as const;

/** Headers for a worker call, including the action-execution shared secret. */
export function workerHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    accept: 'text/event-stream',
    ...extra,
  };
  // Only the worker's action-execution endpoint enforces this, but sending it
  // on every assistant call keeps the client code uniform and costs nothing.
  const token = process.env.INTERNAL_API_TOKEN;
  if (token) headers['x-internal-token'] = token;
  return headers;
}

/** Relay a worker SSE endpoint to the browser unchanged. */
export async function proxyStream(
  request: NextRequest,
  workerPath: string,
  body: string,
): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetch(`${WORKER_URL}${workerPath}`, {
      method: 'POST',
      headers: workerHeaders(),
      body,
      signal: request.signal,
      cache: 'no-store',
    });
  } catch (err) {
    // An aborted request is the user closing the panel, not a failure.
    if (request.signal.aborted) return new Response(null, { status: 499 });
    return NextResponse.json(
      {
        error:
          'The DevServer worker is unreachable. Start it with ' +
          '`cd apps/worker && uv run uvicorn src.main:app --port 8000`, ' +
          'or check WORKER_URL.',
        detail: err instanceof Error ? err.message : String(err),
      },
      { status: 502 },
    );
  }

  if (!upstream.ok || !upstream.body) {
    const detail = await upstream.text().catch(() => '');
    let message = detail;
    try {
      message = JSON.parse(detail)?.detail ?? detail;
    } catch {
      /* not JSON — use the raw text */
    }
    return NextResponse.json(
      { error: message || 'assistant stream failed' },
      { status: upstream.status },
    );
  }

  return new Response(upstream.body, { headers: SSE_HEADERS });
}
