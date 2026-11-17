import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import { parseRouteId } from '@/lib/route-params';
import { cancelTaskFully } from '@/lib/task-cancel';
import { workerErrorResponse } from '@/lib/worker-client';

interface RouteContext {
  params: Promise<{ id: string }>;
}

/**
 * POST /api/tasks/<id>/cancel
 *
 * Drops the queued job and asks the worker to cancel (killing a running
 * agent); see `lib/task-cancel.ts`, shared with the A2A gateway.
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

    const outcome = await cancelTaskFully(taskId, taskResult.rows[0].queue_job_id);
    if (!outcome.ok) return workerErrorResponse(outcome.res, outcome.body);
    if (!outcome.worker_reachable) {
      return NextResponse.json({
        success: true,
        agent_stopped: false,
        worker_reachable: false,
      });
    }
    return NextResponse.json({
      success: true,
      agent_stopped: outcome.agent_stopped,
      worker_reachable: true,
      old_status: outcome.old_status,
      new_status: outcome.new_status,
    });
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/cancel`);
  }
}
