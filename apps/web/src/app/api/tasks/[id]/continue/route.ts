import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { enqueueTask } from '@/lib/queue';
import { apiErrorResponse } from '@/lib/api-errors';
import {
  callWorker,
  invalidId,
  parseRouteId,
  readWorkerJson,
  workerError,
  workerUnreachable,
  type RouteContext,
} from '@/lib/route-utils';

/**
 * POST /api/tasks/<id>/continue   body: { model?, mode? }
 *
 * Resume a task in its existing agent session: the worker marks in-flight
 * runs failed (keeping the session id) and sets the continuation flag, then
 * this route re-enqueues the task. Enqueueing stays here because the web tier
 * is the only queue producer.
 */
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) return invalidId('task id');

  try {
    const body = await request.json().catch(() => ({}));
    const { model, mode } = body as { model?: string; mode?: string };

    // Look up task to get task_key and current state.
    const taskResult = await query(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.id = $1`,
      [taskId],
    );
    if (taskResult.rows.length === 0) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }
    const task = taskResult.rows[0];

    // ``test`` is allowed — operators can leave a note in the Notes panel
    // and continue the same session without reopening.
    if (['done', 'retired'].includes(task.status)) {
      return NextResponse.json(
        { error: `Cannot continue task with status '${task.status}'` },
        { status: 400 },
      );
    }

    // 1. Tell worker to set continuation flag + cancel in-flight runs.
    const workerRes = await callWorker(
      `/internal/tasks/${encodeURIComponent(task.task_key)}/continue`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: model ?? null, mode: mode ?? null }),
      },
    );
    if (!workerRes) return workerUnreachable();
    if (!workerRes.ok) {
      return workerError(workerRes, await readWorkerJson(workerRes));
    }

    // 2. Re-enqueue the task.
    const effectiveMode = mode || task.claude_mode;
    const jobId = await enqueueTask({
      taskId: task.id,
      repoId: task.repo_id,
      taskKey: task.task_key,
      title: task.title,
      priority: task.priority,
      mode: task.mode,
      claudeMode: effectiveMode,
      maxTurns: task.max_turns ?? null,
      gitFlow: task.git_flow ?? 'branch',
    });

    await query(
      `UPDATE tasks SET status = 'queued', queue_job_id = $1, updated_at = NOW() WHERE id = $2`,
      [jobId, taskId],
    );

    return NextResponse.json({ success: true, jobId });
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/continue`);
  }
}
