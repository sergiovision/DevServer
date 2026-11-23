import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { query } from '@/lib/db';
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

const KINDS = new Set(['request', 'response', 'note', 'handoff']);
/** Statuses in which an agent is live and polls its inbox between steps. */
const LIVE_STATUSES = new Set(['running', 'verifying']);

/**
 * POST /api/tasks/<id>/messages/as-operator   body: { body: string, subject?, kind? }
 *
 * Send a message FROM the reserved `operator` key TO this task's inbox, so the
 * human can answer an agent without impersonating another task. Returns
 * `{ message, live }` — `live` is true when the task's agent is running and
 * will see the message on its next inbox check.
 */
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) return invalidId('task id');

  const payload = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  const messageBody = payload?.body;
  if (typeof messageBody !== 'string' || !messageBody.trim()) {
    return jsonError(400, 'body is required');
  }
  const kind = typeof payload?.kind === 'string' && KINDS.has(payload.kind) ? payload.kind : 'request';

  try {
    const result = await query<{ task_key: string; status: string }>(
      'SELECT task_key, status FROM tasks WHERE id = $1',
      [taskId],
    );
    const task = result.rows[0];
    if (!task) {
      return jsonError(404, 'Task not found');
    }

    // The operator lane is separate from the per-task agent lane so a task
    // cannot post as `operator`; it checks INTERNAL_API_TOKEN when set.
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    const token = process.env.INTERNAL_API_TOKEN;
    if (token) headers['x-internal-token'] = token;
    const workerRes = await callWorker('/internal/operator/messages/send', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        to_task_key: task.task_key,
        body: messageBody,
        subject: typeof payload?.subject === 'string' ? payload.subject : null,
        kind,
      }),
    });
    if (workerRes === null) return workerUnreachable();

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) return workerError(workerRes, body);
    return NextResponse.json(
      { message: body.message ?? null, live: LIVE_STATUSES.has(task.status) },
      { status: 201 },
    );
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/messages/as-operator`);
  }
}
