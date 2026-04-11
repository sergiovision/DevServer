/**
 * POST /api/tasks/[id]/approve
 *
 * Approves (or rejects) an interactive-mode task's plan. The Python worker
 * polls `tasks.plan_approved_at` / `plan_rejected_at` in `plan_gate.wait_for_approval`
 * and resumes execution as soon as one is set.
 *
 * Body:
 *   { "action": "approve" }   // default
 *   { "action": "reject" }
 */
import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);

  if (!Number.isFinite(taskId)) {
    return NextResponse.json({ error: 'Invalid task id' }, { status: 400 });
  }

  let action: 'approve' | 'reject' = 'approve';
  try {
    const body = await request.json().catch(() => ({}));
    if (body?.action === 'reject') {
      action = 'reject';
    }
  } catch {
    // no body — default to approve
  }

  try {
    const taskResult = await query(
      `SELECT id, mode, status, plan_approved_at, plan_rejected_at
         FROM tasks WHERE id = $1`,
      [taskId],
    );

    if (taskResult.rows.length === 0) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }

    const task = taskResult.rows[0];

    if (task.mode !== 'interactive') {
      return NextResponse.json(
        { error: `Task ${taskId} is not in interactive mode (mode=${task.mode})` },
        { status: 400 },
      );
    }

    if (task.plan_approved_at || task.plan_rejected_at) {
      return NextResponse.json(
        {
          error: `Plan already ${task.plan_approved_at ? 'approved' : 'rejected'}`,
          approved_at: task.plan_approved_at,
          rejected_at: task.plan_rejected_at,
        },
        { status: 409 },
      );
    }

    if (action === 'approve') {
      await query(
        `UPDATE tasks SET plan_approved_at = NOW(), updated_at = NOW() WHERE id = $1`,
        [taskId],
      );
    } else {
      await query(
        `UPDATE tasks SET plan_rejected_at = NOW(), updated_at = NOW() WHERE id = $1`,
        [taskId],
      );
    }

    return NextResponse.json({ success: true, action });
  } catch (err) {
    console.error(`POST /api/tasks/${id}/approve error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
