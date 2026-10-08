/**
 * GET /api/import/confluence/item/[id]?repo_id=
 *
 * Proxy to the worker's /internal/import/confluence/item/{id} — one page
 * fetched and normalised to a Markdown draft (used by the preview pane).
 */
import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function GET(req: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  try {
    const qs = req.nextUrl.searchParams.toString();
    const res = await fetch(
      `${WORKER_URL}/internal/import/confluence/item/${encodeURIComponent(id)}${qs ? `?${qs}` : ''}`,
      { cache: 'no-store' },
    );
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}
