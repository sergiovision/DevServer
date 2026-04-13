import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function GET(_request: NextRequest, context: RouteContext) {
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

    const runsResult = await query(
      'SELECT * FROM task_runs WHERE task_id = $1 ORDER BY attempt DESC',
      [taskId],
    );

    return NextResponse.json({
      ...taskResult.rows[0],
      runs: runsResult.rows,
    });
  } catch (err) {
    console.error(`GET /api/tasks/${id} error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);

  try {
    const body = await request.json();
    const allowed = [
      'task_key', 'title', 'description', 'acceptance',
      'priority', 'labels', 'mode', 'claude_mode', 'agent_vendor', 'claude_model', 'max_turns', 'skip_verify', 'git_flow', 'backup_model', 'status', 'depends_on', 'queue_job_id',
    ];

    if (body.agent_vendor !== undefined) {
      const ALLOWED_VENDORS = ['anthropic', 'google', 'openai', 'glm'];
      if (!ALLOWED_VENDORS.includes(body.agent_vendor)) {
        return NextResponse.json(
          { error: `agent_vendor must be one of: ${ALLOWED_VENDORS.join(', ')}` },
          { status: 400 },
        );
      }
    }

    if (body.max_turns !== undefined && body.max_turns !== null &&
        (!Number.isInteger(body.max_turns) || body.max_turns <= 0)) {
      return NextResponse.json({ error: 'max_turns must be a positive integer or null' }, { status: 400 });
    }

    const sets: string[] = [];
    const values: unknown[] = [];
    let paramIndex = 1;

    for (const key of allowed) {
      if (body[key] !== undefined) {
        sets.push(`${key} = $${paramIndex++}`);
        values.push(body[key]);
      }
    }

    if (sets.length === 0) {
      return NextResponse.json({ error: 'No valid fields to update' }, { status: 400 });
    }

    sets.push(`updated_at = NOW()`);
    values.push(taskId);

    const result = await query(
      `UPDATE tasks SET ${sets.join(', ')} WHERE id = $${paramIndex} RETURNING *`,
      values,
    );

    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }

    return NextResponse.json(result.rows[0]);
  } catch (err) {
    console.error(`PATCH /api/tasks/${id} error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export async function DELETE(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);

  try {
    const result = await query('DELETE FROM tasks WHERE id = $1 RETURNING id', [taskId]);
    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Task not found' }, { status: 404 });
    }
    return NextResponse.json({ deleted: true, id: taskId });
  } catch (err) {
    console.error(`DELETE /api/tasks/${id} error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
