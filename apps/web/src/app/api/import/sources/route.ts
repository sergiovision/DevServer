/**
 * GET /api/import/sources
 *
 * Proxy to the worker's /internal/import/sources — the registered external
 * import sources (Confluence, …) and whether each is configured. Pass
 * ?repo_id= to evaluate per-repo credential overrides.
 */
import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function GET(req: NextRequest) {
  try {
    const qs = req.nextUrl.searchParams.toString();
    const res = await fetch(
      `${WORKER_URL}/internal/import/sources${qs ? `?${qs}` : ''}`,
      { cache: 'no-store' },
    );
    const data = await res.json().catch(() => []);
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}
