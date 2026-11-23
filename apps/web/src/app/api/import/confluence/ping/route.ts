/**
 * GET /api/import/confluence/ping?repo_id=
 *
 * Proxy to the worker's /internal/import/confluence/ping — connectivity +
 * auth probe. Always resolves to {ok, error?}: the Settings page renders the
 * body whatever the status, so this route relays it verbatim instead of
 * reshaping errors the way `proxyWorker` does.
 */
import { NextRequest, NextResponse } from 'next/server';
import { callWorker, WORKER_UNREACHABLE_MESSAGE } from '@/lib/route-utils';

// The probe makes a round trip to Confluence.
const CONFLUENCE_TIMEOUT_MS = 60_000;

export async function GET(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  const res = await callWorker(
    `/internal/import/confluence/ping${qs ? `?${qs}` : ''}`,
    {},
    CONFLUENCE_TIMEOUT_MS,
  );
  if (!res) {
    return NextResponse.json({ ok: false, error: WORKER_UNREACHABLE_MESSAGE }, { status: 502 });
  }
  const data: unknown = await res.json().catch(() => ({ ok: false, error: 'bad worker response' }));
  return NextResponse.json(data, { status: res.status });
}
