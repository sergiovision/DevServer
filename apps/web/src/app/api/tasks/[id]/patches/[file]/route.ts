import { NextRequest } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { isValidPatchFileName } from '@/lib/patch-files';
import { findTaskKey } from '@/lib/tasks';
import {
  callWorker,
  invalidId,
  jsonError,
  parseRouteId,
  readWorkerJson,
  workerError,
  workerUnreachable,
  type RouteContext,
} from '@/lib/route-utils';

/**
 * GET /api/tasks/<id>/patches/<file>
 *
 * Download one exported patch. The worker's file body is streamed straight
 * through rather than buffered, with headers that make the browser save it.
 */
export async function GET(_request: NextRequest, context: RouteContext<{ id: string; file: string }>) {
  const { id, file } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) return invalidId('task id');
  if (!isValidPatchFileName(file)) {
    return jsonError(400, 'Invalid patch file name');
  }

  try {
    const taskKey = await findTaskKey(taskId);
    if (taskKey === null) {
      return jsonError(404, 'Task not found');
    }

    const workerRes = await callWorker(
      `/internal/tasks/${encodeURIComponent(taskKey)}/patches/${encodeURIComponent(file)}`,
    );
    if (workerRes === null) return workerUnreachable();
    if (!workerRes.ok) {
      return workerError(workerRes, await readWorkerJson(workerRes));
    }

    return new Response(workerRes.body, {
      status: 200,
      headers: {
        'Content-Type': 'text/x-patch; charset=utf-8',
        // The name already passed the strict pattern above, so it is safe to
        // place inside the quoted header value as-is.
        'Content-Disposition': `attachment; filename="${file}"`,
        'Cache-Control': 'no-store',
      },
    });
  } catch (err) {
    return apiErrorResponse(err, `GET /api/tasks/${id}/patches/${file}`);
  }
}
