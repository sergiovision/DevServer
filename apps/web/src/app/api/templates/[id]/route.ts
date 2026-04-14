import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  try {
    const result = await query('SELECT * FROM task_templates WHERE id = $1', [parseInt(id)]);
    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Template not found' }, { status: 404 });
    }
    return NextResponse.json(result.rows[0]);
  } catch (err) {
    console.error(`GET /api/templates/${id} error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const templateId = parseInt(id);

  try {
    const body = await request.json();
    const allowed = [
      'name', 'description', 'acceptance', 'git_flow', 'claude_mode',
      'agent_vendor', 'claude_model', 'backup_vendor', 'backup_model',
      'max_turns', 'skip_verify',
    ];

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
    values.push(templateId);

    const result = await query(
      `UPDATE task_templates SET ${sets.join(', ')} WHERE id = $${paramIndex} RETURNING *`,
      values,
    );

    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Template not found' }, { status: 404 });
    }

    return NextResponse.json(result.rows[0]);
  } catch (err) {
    console.error(`PATCH /api/templates/${id} error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export async function DELETE(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  try {
    const result = await query('DELETE FROM task_templates WHERE id = $1 RETURNING id', [parseInt(id)]);
    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Template not found' }, { status: 404 });
    }
    return NextResponse.json({ deleted: true, id: parseInt(id) });
  } catch (err) {
    console.error(`DELETE /api/templates/${id} error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
