/**
 * GET /api/import/confluence/search?q=&scope=&limit=&repo_id=
 *
 * Proxy to the worker's /internal/import/confluence/search — CQL or free-text
 * search (prefix the query with "cql:" for raw CQL).
 */
import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function GET(req: NextRequest) {
  try {
    const qs = req.nextUrl.searchParams.toString();
    const res = await fetch(
      `${WORKER_URL}/internal/import/confluence/search${qs ? `?${qs}` : ''}`,
      { cache: 'no-store' },
    );
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}
