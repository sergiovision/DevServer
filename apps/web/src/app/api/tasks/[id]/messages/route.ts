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

/** Resolve the route's task id to its task key, or an error response. */
async function resolveTaskKey(rawId: string): Promise<string | NextResponse> {
  const taskId = parseRouteId(rawId);
  if (taskId === null) {
    return NextResponse.json({ error: 'Invalid task id' }, { status: 400 });
  }
  const taskKey = await findTaskKey(taskId);
  if (taskKey === null) {
    return NextResponse.json({ error: 'Task not found' }, { status: 404 });
  }
  return taskKey;
}

/**
 * GET /api/tasks/<id>/messages?limit=50
 *
 * The task's inter-task message thread in both directions, oldest first:
 * `{ messages: [{ id, from_task_key, to_task_key, kind, subject, body, read_at, created_at, … }] }`.
 */
export async function GET(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const rawLimit = Number(request.nextUrl.searchParams.get('limit') ?? '50');
  const limit = Number.isInteger(rawLimit) && rawLimit > 0 ? Math.min(rawLimit, 500) : 50;
  try {
    const taskKey = await resolveTaskKey(id);
    if (taskKey instanceof NextResponse) return taskKey;

    const workerRes = await callWorker(
      `/internal/tasks/${encodeURIComponent(taskKey)}/messages/thread?limit=${limit}`,
    );
    if (workerRes === null) return workerUnreachableResponse();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerErrorResponse(workerRes, body);
    return NextResponse.json({ messages: Array.isArray(body.messages) ? body.messages : [] });
  } catch (err) {
    return apiErrorResponse(err, `GET /api/tasks/${id}/messages`);
  }
}

/**
 * POST /api/tasks/<id>/messages
 *   body: { to_task_key: string, body: string, subject?, kind?, payload? }
 *
 * Send a message AS this task to another task (or to `operator`).
 */
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;

  const payload = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  const toTaskKey = payload?.to_task_key;
  const messageBody = payload?.body;
  if (typeof toTaskKey !== 'string' || !toTaskKey.trim()) {
    return NextResponse.json({ error: 'to_task_key is required' }, { status: 400 });
  }
  if (typeof messageBody !== 'string' || !messageBody.trim()) {
    return NextResponse.json({ error: 'body is required' }, { status: 400 });
  }

  try {
    const taskKey = await resolveTaskKey(id);
    if (taskKey instanceof NextResponse) return taskKey;

    const workerRes = await callWorker(
      `/internal/tasks/${encodeURIComponent(taskKey)}/messages/send`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          to_task_key: toTaskKey.trim(),
          body: messageBody,
          subject: typeof payload?.subject === 'string' ? payload.subject : null,
          kind: typeof payload?.kind === 'string' ? payload.kind : 'note',
          payload:
            payload?.payload &&
            typeof payload.payload === 'object' &&
            !Array.isArray(payload.payload)
              ? payload.payload
              : null,
        }),
      },
    );
    if (workerRes === null) return workerUnreachableResponse();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerErrorResponse(workerRes, body);
    return NextResponse.json({ message: body.message ?? null }, { status: 201 });
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/messages`);
  }
}
