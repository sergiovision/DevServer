/**
 * GET /api/codex-sessions/[id]/tail?since=<byte-offset>
 *
 * Proxy to the worker's transcript tail. Same byte-offset protocol the Logs
 * page uses: pass back the `next_offset` you were given and you get only what
 * was appended since.
 */
import { NextRequest, NextResponse } from 'next/server';
import { callWorker, readWorkerJson } from '@/lib/worker-client';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function GET(req: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  const since = req.nextUrl.searchParams.get('since') ?? '0';
  const nextOffset = Number(since) || 0;

  const res = await callWorker(
    `/internal/codex-sessions/${encodeURIComponent(id)}/tail` +
      `?since=${encodeURIComponent(since)}`,
  );
  if (res === null) {
    return NextResponse.json(
      {
        events: [],
        next_offset: nextOffset,
        error: 'Worker unreachable',
        hint: `Could not reach the worker at ${WORKER_URL}.`,
      },
      { status: 502 },
    );
  }

  const data = await readWorkerJson(res);

  if (!res.ok) {
    const error = res.status === 404 ? 'Codex rollout not found' : `Worker returned ${res.status}`;
    const hint =
      res.status === 404
        ? 'The rollout may have been cleaned out of ~/.codex/sessions.'
        : data.detail || data.error || 'Check logs/worker.log for the traceback.';
    return NextResponse.json(
      { events: [], next_offset: nextOffset, error, hint },
      { status: res.status },
    );
  }

  return NextResponse.json(data);
}
