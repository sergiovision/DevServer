/**
 * GET /api/import/confluence/ping?repo_id=
 *
 * Proxy to the worker's /internal/import/confluence/ping — connectivity +
 * auth probe. Always resolves to {ok, error?}.
 */
import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function GET(req: NextRequest) {
  try {
    const qs = req.nextUrl.searchParams.toString();
    const res = await fetch(
      `${WORKER_URL}/internal/import/confluence/ping${qs ? `?${qs}` : ''}`,
      { cache: 'no-store' },
    );
    const data = await res.json().catch(() => ({ ok: false, error: 'bad worker response' }));
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ ok: false, error: 'worker unreachable' }, { status: 502 });
  }
}
