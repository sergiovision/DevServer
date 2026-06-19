import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

/**
 * POST /api/env/test-db
 *
 * Proxies an unsaved DB-connection test to the worker, which opens a
 * short-lived connection with the supplied credentials and reports the result.
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const res = await fetch(`${WORKER_URL}/internal/env/test-db`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return NextResponse.json(data, { status: res.ok ? 200 : res.status });
  } catch (err) {
    console.error('POST /api/env/test-db error:', err);
    return NextResponse.json(
      { ok: false, error: 'Could not reach the worker to test the connection.' },
      { status: 502 },
    );
  }
}
