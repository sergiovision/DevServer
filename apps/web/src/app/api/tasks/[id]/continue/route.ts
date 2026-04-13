import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { enqueueTask } from '@/lib/queue';
import { WORKER_URL } from '@/lib/worker-url';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);

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

    if (['done', 'test', 'retired'].includes(task.status)) {
      return NextResponse.json(
        { error: `Cannot continue task with status '${task.status}'` },
        { status: 400 },
      );
    }

    // 1. Tell worker to set continuation flag + cancel in-flight runs.
    const workerRes = await fetch(
      `${WORKER_URL}/internal/tasks/${encodeURIComponent(task.task_key)}/continue`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: model ?? null, mode: mode ?? null }),
      },
    );
    if (!workerRes.ok) {
      const detail = await workerRes.json().catch(() => ({}));
      return NextResponse.json(
        { error: detail.detail || 'Worker continue failed' },
        { status: workerRes.status },
      );
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
    console.error(`POST /api/tasks/${id}/continue error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
