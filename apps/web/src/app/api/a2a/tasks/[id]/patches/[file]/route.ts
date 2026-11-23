import { NextRequest } from 'next/server';
import { peerWorkerError, resolvePeerTask } from '@/lib/a2a/peer-task';
import { isValidPatchFileName } from '@/lib/patch-files';
import { callWorker, jsonError, workerUnreachable, type RouteContext } from '@/lib/route-utils';

/**
 * GET /api/a2a/tasks/<id>/patches/<file>     Authorization: Bearer <peer token>
 *
 * Peer-scoped download of one exported patch, streamed through from the
 * worker.
 *
 * The `[id]` segment is parsed by `resolvePeerTask` (malformed → the same 404
 * as a foreign task). Errors stay generic because the caller is external.
 */
export async function GET(request: NextRequest, context: RouteContext<{ id: string; file: string }>) {
  const { id, file } = await context.params;
  try {
    const resolved = await resolvePeerTask(request, id);
    if (!resolved.ok) return resolved.response;
    if (!isValidPatchFileName(file)) {
      return jsonError(400, 'Invalid patch file name');
    }

    const workerRes = await callWorker(
      `/internal/tasks/${encodeURIComponent(resolved.taskKey)}/patches/${encodeURIComponent(file)}`,
    );
    if (workerRes === null) return workerUnreachable();
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
    return jsonError(500, 'Internal error');
  }
}
