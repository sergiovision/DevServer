/**
 * GET /api/codex-sessions
 *
 * Proxy to the worker's /internal/codex-sessions — the list of OpenAI Codex CLI
 * sessions running (or recently run) on the worker host.
 *
 * Failures are surfaced with a classified diagnostic rather than an empty
 * list: "no sessions" and "the worker is down" look identical in the UI
 * otherwise, and the second one is the operator's problem to fix.
 */
import { NextRequest, NextResponse } from 'next/server';
import { callWorker, readWorkerJson } from '@/lib/worker-client';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

export async function GET(req: NextRequest) {
  const limit = req.nextUrl.searchParams.get('limit') ?? '60';
  const liveOnly = req.nextUrl.searchParams.get('live_only') ?? 'false';

  const res = await callWorker(
    `/internal/codex-sessions?limit=${encodeURIComponent(limit)}` +
      `&live_only=${encodeURIComponent(liveOnly)}`,
  );
  if (res === null) {
    return NextResponse.json(
      {
        sessions: [],
        error: 'Worker unreachable',
        hint: `Could not reach the worker at ${WORKER_URL}. Start it with ` +
          '`cd apps/worker && uv run uvicorn src.main:app --port 8000`.',
      },
      { status: 502 },
    );
  }

  const data = await readWorkerJson(res);
  if (!res.ok) {
    return NextResponse.json(
      {
        sessions: [],
        error: `Worker returned ${res.status}`,
        hint: data.detail || data.error || 'Check logs/worker.log for the traceback.',
      },
      { status: res.status },
    );
  }

  return NextResponse.json(data);
}
