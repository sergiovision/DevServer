import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
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
 * GET /api/tasks/<id>/patches
 *
 * The `.patch` files a patch-mode task exported: `{ patches: [{ name, size_bytes }] }`.
 */
export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) return invalidId('task id');

  try {
    const taskKey = await findTaskKey(taskId);
    if (taskKey === null) {
      return jsonError(404, 'Task not found');
    }

    const workerRes = await callWorker(`/internal/tasks/${encodeURIComponent(taskKey)}/patches`);
    if (workerRes === null) return workerUnreachable();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerError(workerRes, body);
    return NextResponse.json({ patches: Array.isArray(body.patches) ? body.patches : [] });
  } catch (err) {
    return apiErrorResponse(err, `GET /api/tasks/${id}/patches`);
  }
}
