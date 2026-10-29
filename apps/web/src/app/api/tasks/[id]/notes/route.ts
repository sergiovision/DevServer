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

/** Resolve the route's task id to the worker's notes URL, or an error response. */
async function notesPath(rawId: string): Promise<string | NextResponse> {
  const taskId = parseRouteId(rawId);
  if (taskId === null) {
    return NextResponse.json({ error: 'Invalid task id' }, { status: 400 });
  }
  const taskKey = await findTaskKey(taskId);
  if (taskKey === null) {
    return NextResponse.json({ error: 'Task not found' }, { status: 404 });
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
    if (workerRes === null) return workerUnreachableResponse();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerErrorResponse(workerRes, body);
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
    return NextResponse.json({ error: 'body is required' }, { status: 400 });
  }

  try {
    const path = await notesPath(id);
    if (path instanceof NextResponse) return path;

    const workerRes = await callWorker(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body: noteBody.trim() }),
    });
    if (workerRes === null) return workerUnreachableResponse();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerErrorResponse(workerRes, body);
    return NextResponse.json(
      { id: body.id, will_be_delivered: body.will_be_delivered === true },
      { status: 201 },
    );
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/notes`);
  }
}
