import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { cancelTask } from '@/lib/queue';
import { apiErrorResponse } from '@/lib/api-errors';
import { parseRouteId } from '@/lib/route-params';
import {
  callWorker,
  readWorkerJson,
  workerErrorResponse,
} from '@/lib/worker-client';

interface RouteContext {
  params: Promise<{ id: string }>;
}

/**
 * POST /api/tasks/<id>/cancel
 *
 * Cancelling has two halves:
 *   1. drop the queued PgQueuer job, so a task that has not started never does;
 *   2. ask the worker to cancel the task, which also kills a running agent
 *      process — a status flip in the database alone would leave the CLI
 *      running and spending tokens.
 *
 * When the worker is down there is no agent to kill, so the status is set
 * directly in the database instead.
 */
export async function POST(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) {
    return NextResponse.json({ error: 'Invalid task id' }, { status: 400 });
  }

  try {
    const taskResult = await query<{ queue_job_id: string | null }>(
      'SELECT queue_job_id FROM tasks WHERE id = $1',
      [taskId],
    );
    if (taskResult.rows.length === 0) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }

    await dropQueuedJob(taskResult.rows[0].queue_job_id);

    const workerRes = await callWorker(`/internal/cancel/${taskId}`, { method: 'POST' });
    if (workerRes === null) {
      await markCancelledInDatabase(taskId);
      return NextResponse.json({
        success: true,
        agent_stopped: false,
        worker_reachable: false,
      });
    }

    const body = await readWorkerJson(workerRes);
    if (!workerRes.ok) {
      return workerErrorResponse(workerRes, body);
    }
    return NextResponse.json({
      success: true,
      agent_stopped: body.agent_stopped === true,
      worker_reachable: true,
      old_status: body.old_status ?? null,
      new_status: body.new_status ?? 'cancelled',
    });
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/cancel`);
  }
}

/** Remove the task's job from the queue if it has not been picked up yet. */
async function dropQueuedJob(queueJobId: string | null): Promise<void> {
  if (!queueJobId) return;
  try {
    await cancelTask(queueJobId);
  } catch {
    // The job may already be gone; the cancellation itself still proceeds.
  }
}

/** Fallback used only when the worker is unreachable. */
async function markCancelledInDatabase(taskId: number): Promise<void> {
  await query(
    `UPDATE tasks SET status = 'cancelled', updated_at = NOW() WHERE id = $1`,
    [taskId],
  );
}
