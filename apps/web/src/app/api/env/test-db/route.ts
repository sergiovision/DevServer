import { NextRequest, NextResponse } from 'next/server';
import { callWorker } from '@/lib/route-utils';

/**
 * POST /api/env/test-db
 *
 * Proxies an unsaved DB-connection test to the worker, which opens a
 * short-lived connection with the supplied credentials and reports the result.
 *
 * Always answers `{ok, error?}` (the wizard renders that body whatever the
 * status), so the worker's body is relayed verbatim rather than reshaped by
 * `proxyWorker`.
 */

// A connection attempt to an unreachable host waits out its own timeout first.
const TEST_DB_TIMEOUT_MS = 30_000;

export async function POST(request: NextRequest) {
  const body: unknown = await request.json().catch(() => null);
  if (body === null) {
    return NextResponse.json({ ok: false, error: 'Invalid JSON body' }, { status: 400 });
  }
  const res = await callWorker(
    '/internal/env/test-db',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
    TEST_DB_TIMEOUT_MS,
  );
  if (!res) {
    return NextResponse.json(
      { ok: false, error: 'Could not reach the worker to test the connection.' },
      { status: 502 },
    );
  }
  const data: unknown = await res.json().catch(() => ({ ok: false, error: 'bad worker response' }));
  return NextResponse.json(data, { status: res.ok ? 200 : res.status });
}
