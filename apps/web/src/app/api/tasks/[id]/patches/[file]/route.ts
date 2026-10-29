import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { isValidPatchFileName } from '@/lib/patch-files';
import { parseRouteId } from '@/lib/route-params';
import { findTaskKey } from '@/lib/tasks';
import {
  callWorker,
  readWorkerJson,
  workerErrorResponse,
  workerUnreachableResponse,
} from '@/lib/worker-client';

interface RouteContext {
  params: Promise<{ id: string; file: string }>;
}

/**
 * GET /api/tasks/<id>/patches/<file>
 *
 * Download one exported patch. The worker's file body is streamed straight
 * through rather than buffered, with headers that make the browser save it.
 */
export async function GET(_request: NextRequest, context: RouteContext) {
  const { id, file } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) {
    return NextResponse.json({ error: 'Invalid task id' }, { status: 400 });
  }
  if (!isValidPatchFileName(file)) {
    return NextResponse.json({ error: 'Invalid patch file name' }, { status: 400 });
  }

  try {
    const taskKey = await findTaskKey(taskId);
    if (taskKey === null) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }

    const workerRes = await callWorker(
      `/internal/tasks/${encodeURIComponent(taskKey)}/patches/${encodeURIComponent(file)}`,
    );
    if (workerRes === null) return workerUnreachableResponse();
    if (!workerRes.ok) {
      return workerErrorResponse(workerRes, await readWorkerJson(workerRes));
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
