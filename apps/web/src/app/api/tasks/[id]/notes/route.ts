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

/** Resolve the route's task id to the worker's notes URL, or an error response. */
async function notesPath(rawId: string): Promise<string | NextResponse> {
  const taskId = parseRouteId(rawId);
  if (taskId === null) return invalidId('task id');
  const taskKey = await findTaskKey(taskId);
  if (taskKey === null) {
    return jsonError(404, 'Task not found');
  }
  return `/internal/tasks/${encodeURIComponent(taskKey)}/notes`;
}

/**
 * GET /api/tasks/<id>/notes
 *
 * Operator notes left for the task's agent:
 * `{ notes: [{ id, body, created_at, delivered_at }] }`.
 */
export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  try {
    const path = await notesPath(id);
    if (path instanceof NextResponse) return path;

    const workerRes = await callWorker(path);
    if (workerRes === null) return workerUnreachable();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerError(workerRes, body);
    return NextResponse.json({ notes: Array.isArray(body.notes) ? body.notes : [] });
  } catch (err) {
    return apiErrorResponse(err, `GET /api/tasks/${id}/notes`);
  }
}

/**
 * POST /api/tasks/<id>/notes   body: { body: string }
 *
 * Leave a note for the agent. Returns `{ id, will_be_delivered }` — false when
 * the task is not running, in which case the note is kept for the record only.
 */
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;

  const payload: unknown = await request.json().catch(() => null);
  const noteBody = (payload as { body?: unknown } | null)?.body;
  if (typeof noteBody !== 'string' || !noteBody.trim()) {
    return jsonError(400, 'body is required');
  }

  try {
    const path = await notesPath(id);
    if (path instanceof NextResponse) return path;

    const workerRes = await callWorker(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body: noteBody.trim() }),
    });
    if (workerRes === null) return workerUnreachable();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerError(workerRes, body);
    return NextResponse.json(
      { id: body.id, will_be_delivered: body.will_be_delivered === true },
      { status: 201 },
    );
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/notes`);
  }
}
