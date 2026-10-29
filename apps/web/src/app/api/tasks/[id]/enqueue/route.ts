import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { enqueueStoredTask } from '@/lib/tasks';
import { apiErrorResponse } from '@/lib/api-errors';
import type { Task } from '@/lib/types';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function POST(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);

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

    const jobId = await enqueueStoredTask(task);

    return NextResponse.json({ success: true, jobId });
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/enqueue`);
  }
}
