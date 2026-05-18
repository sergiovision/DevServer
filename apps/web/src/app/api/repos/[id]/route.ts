import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseInt(id);

  try {
    const result = await query('SELECT * FROM repos WHERE id = $1', [repoId]);
    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Repo not found' }, { status: 404 });
    }
    return NextResponse.json(result.rows[0]);
  } catch (err) {
    return apiErrorResponse(err, `GET /api/repos/${id}`);
  }
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseInt(id);

  try {
    const body = await request.json();
    const allowed = [
      'name', 'gitea_url', 'gitea_owner', 'gitea_repo', 'clone_url',
      'default_branch', 'build_cmd', 'test_cmd', 'lint_cmd', 'pre_cmd',
      'claude_model', 'claude_allowed_tools', 'gitea_token', 'provider',
      'max_retries', 'timeout_minutes', 'active',
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
    values.push(repoId);

    const result = await query(
      `UPDATE repos SET ${sets.join(', ')} WHERE id = $${paramIndex} RETURNING *`,
      values,
    );

    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Repo not found' }, { status: 404 });
    }

    return NextResponse.json(result.rows[0]);
  } catch (err) {
    return apiErrorResponse(err, `PATCH /api/repos/${id}`);
  }
}

export async function DELETE(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseInt(id);

  try {
    const result = await query('DELETE FROM repos WHERE id = $1 RETURNING id', [repoId]);
    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Repo not found' }, { status: 404 });
    }
    return NextResponse.json({ deleted: true, id: repoId });
  } catch (err) {
    return apiErrorResponse(err, `DELETE /api/repos/${id}`);
  }
}
