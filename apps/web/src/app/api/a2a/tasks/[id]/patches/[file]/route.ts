import { NextRequest, NextResponse } from 'next/server';
import { peerWorkerError, resolvePeerTask } from '@/lib/a2a/peer-task';
import { isValidPatchFileName } from '@/lib/patch-files';
import { callWorker, workerUnreachableResponse } from '@/lib/worker-client';

interface RouteContext {
  params: Promise<{ id: string; file: string }>;
}

/**
 * GET /api/a2a/tasks/<id>/patches/<file>     Authorization: Bearer <peer token>
 *
 * Peer-scoped download of one exported patch, streamed through from the
 * worker.
 */
export async function GET(request: NextRequest, context: RouteContext) {
  const { id, file } = await context.params;
  try {
    const resolved = await resolvePeerTask(request, id);
    if (!resolved.ok) return resolved.response;
    if (!isValidPatchFileName(file)) {
      return NextResponse.json({ error: 'Invalid patch file name' }, { status: 400 });
    }

    const workerRes = await callWorker(
      `/internal/tasks/${encodeURIComponent(resolved.taskKey)}/patches/${encodeURIComponent(file)}`,
    );
    if (workerRes === null) return workerUnreachableResponse();
    if (!workerRes.ok) return peerWorkerError(workerRes);

    return new Response(workerRes.body, {
      status: 200,
      headers: {
        'Content-Type': 'text/x-patch; charset=utf-8',
        // The name already passed the strict pattern above.
        'Content-Disposition': `attachment; filename="${file}"`,
        'Cache-Control': 'no-store',
      },
    });
  } catch (err) {
    console.error(`GET /api/a2a/tasks/${id}/patches/${file} error:`, err);
    return NextResponse.json({ error: 'Internal error' }, { status: 500 });
  }
}
