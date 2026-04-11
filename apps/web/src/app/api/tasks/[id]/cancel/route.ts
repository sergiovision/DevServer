import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { cancelTask } from '@/lib/queue';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function POST(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);

  try {
    const taskResult = await query('SELECT * FROM tasks WHERE id = $1', [taskId]);

    if (taskResult.rows.length === 0) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }

    const task = taskResult.rows[0];

    if (task.queue_job_id) {
      try {
        await cancelTask(task.queue_job_id);
      } catch {
        // Job may already be gone
      }
    }

    await query(
      `UPDATE tasks SET status = 'cancelled', updated_at = NOW() WHERE id = $1`,
      [taskId],
    );

    return NextResponse.json({ success: true });
  } catch (err) {
    console.error(`POST /api/tasks/${id}/cancel error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
