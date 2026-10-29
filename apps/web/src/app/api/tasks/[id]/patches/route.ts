import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { parseRouteId } from '@/lib/route-params';
import { findTaskKey } from '@/lib/tasks';
import {
  callWorker,
  readWorkerJson,
  workerErrorResponse,
  workerUnreachableResponse,
} from '@/lib/worker-client';

interface RouteContext {
  params: Promise<{ id: string }>;
}

/**
 * GET /api/tasks/<id>/patches
 *
 * The `.patch` files a patch-mode task exported: `{ patches: [{ name, size_bytes }] }`.
 */
export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) {
    return NextResponse.json({ error: 'Invalid task id' }, { status: 400 });
  }

  try {
    const taskKey = await findTaskKey(taskId);
    if (taskKey === null) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }

    const workerRes = await callWorker(`/internal/tasks/${encodeURIComponent(taskKey)}/patches`);
    if (workerRes === null) return workerUnreachableResponse();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerErrorResponse(workerRes, body);
    return NextResponse.json({ patches: Array.isArray(body.patches) ? body.patches : [] });
  } catch (err) {
    return apiErrorResponse(err, `GET /api/tasks/${id}/patches`);
  }
}
