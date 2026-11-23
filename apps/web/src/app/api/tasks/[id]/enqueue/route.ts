import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { enqueueStoredTask } from '@/lib/tasks';
import { apiErrorResponse } from '@/lib/api-errors';
import { invalidId, parseRouteId, type RouteContext } from '@/lib/route-utils';
import type { Task } from '@/lib/types';

/**
 * POST /api/tasks/<id>/enqueue
 *
 * Put a pending / failed / test task on the queue. Also the worker's
 * callback for worker-initiated enqueues (`queue_bridge.enqueue_via_web`),
 * since the web tier is the only queue producer.
 *
 * Any enqueue not marked `X-DevServer-Triggered` (set only by the worker's
 * post-completion chain) is a fresh start: the stale `triggered_by_task_id`
 * link is cleared so an old chain cannot trip the loop guard's depth cap.
 */
const TRIGGERED_ENQUEUE_HEADER = 'x-devserver-triggered';
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) return invalidId('task id');

  try {
    const taskResult = await query<Task>(
      `SELECT t.*, r.name as repo_name FROM tasks t
       LEFT JOIN repos r ON r.id = t.repo_id
       WHERE t.id = $1`,
      [taskId],
    );

    if (taskResult.rows.length === 0) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }

    const task = taskResult.rows[0];

    if (task.status !== 'pending' && task.status !== 'failed' && task.status !== 'test') {
      return NextResponse.json(
        { error: `Cannot enqueue task with status '${task.status}'` },
        { status: 400 },
      );
    }

    // Only when the column exists and is set: deployments that have not
    // applied migration 004 must keep enqueueing.
    if (task.triggered_by_task_id != null && request.headers.get(TRIGGERED_ENQUEUE_HEADER) !== '1') {
      await query(`UPDATE tasks SET triggered_by_task_id = NULL WHERE id = $1`, [taskId]);
    }

    const jobId = await enqueueStoredTask(task);

    return NextResponse.json({ success: true, jobId });
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/enqueue`);
  }
}
