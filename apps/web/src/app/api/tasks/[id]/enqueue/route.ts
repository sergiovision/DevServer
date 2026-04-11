import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { enqueueTask } from '@/lib/queue';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function POST(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);

  try {
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

    if (task.status !== 'pending' && task.status !== 'failed' && task.status !== 'test') {
      return NextResponse.json(
        { error: `Cannot enqueue task with status '${task.status}'` },
        { status: 400 },
      );
    }

    const jobId = await enqueueTask({
      taskId: task.id,
      repoId: task.repo_id,
      taskKey: task.task_key,
      title: task.title,
      priority: task.priority,
      mode: task.mode,
      claudeMode: task.claude_mode,
      maxTurns: task.max_turns ?? null,
      gitFlow: task.git_flow ?? 'branch',
    });

    await query(
      `UPDATE tasks SET status = 'queued', queue_job_id = $1, updated_at = NOW() WHERE id = $2`,
      [jobId, taskId],
    );

    return NextResponse.json({ success: true, jobId });
  } catch (err) {
    console.error(`POST /api/tasks/${id}/enqueue error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
