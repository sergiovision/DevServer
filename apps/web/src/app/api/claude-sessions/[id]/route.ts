/**
 * DELETE /api/claude-sessions/[id]
 *
 * Erases a Claude session from the worker host's disk — transcript,
 * session-env and file-history. Irreversible; the caller is expected to have
 * confirmed with the operator first.
 *
 * 409 is the interesting status: the worker refuses to delete a session that
 * is still running (live registry entry, a recent write, or a DevServer task
 * in queued/running/verifying that owns it), because its CLI holds the
 * transcript open and would lose both its history and its ability to
 * --resume. 503: the task lookup failed, so the worker refused to guess.
 * 500: nothing could be removed (the per-path errors are in the hint).
 */
import { NextRequest, NextResponse } from 'next/server';
import { callWorker, readWorkerJson } from '@/lib/worker-client';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function DELETE(_req: NextRequest, { params }: RouteContext) {
  const { id } = await params;

  const res = await callWorker(
    `/internal/claude-sessions/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
  );
  if (res === null) {
    return NextResponse.json(
      {
        error: 'Worker unreachable',
        hint: `Could not reach the worker at ${WORKER_URL}. Nothing was deleted.`,
      },
      { status: 502 },
    );
  }

  const data = await readWorkerJson(res);

  if (!res.ok) {
    const detail = data.detail || data.error;
    const error =
      res.status === 409 ? 'Session is still running'
      : res.status === 404 ? 'Nothing to delete'
      : res.status === 500 && detail ? 'Delete failed'
      : `Worker returned ${res.status}`;
    const hint =
      res.status === 409
        ? `${detail ? `${detail}. ` : ''}Stop the session first, then delete it. Nothing was deleted.`
        : detail || 'Check logs/worker.log for the traceback.';
    return NextResponse.json({ error, hint }, { status: res.status });
  }

  return NextResponse.json(data);
}
