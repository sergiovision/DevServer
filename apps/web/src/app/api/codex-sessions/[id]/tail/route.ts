/**
 * GET /api/codex-sessions/[id]/tail?since=<byte-offset>
 *
 * Proxy to the worker's transcript tail. Same byte-offset protocol the Logs
 * page uses: pass back the `next_offset` you were given and you get only what
 * was appended since.
 *
 * `[id]` is a vendor session id (a UUID, not a row id), so it is only
 * URL-encoded; the worker validates it.
 */
import { NextRequest, NextResponse } from 'next/server';
import {
  callWorker,
  readWorkerJson,
  WORKER_UNREACHABLE_MESSAGE,
  type RouteContext,
} from '@/lib/route-utils';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

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
        error: WORKER_UNREACHABLE_MESSAGE,
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
