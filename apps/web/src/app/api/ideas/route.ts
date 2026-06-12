import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';

export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    const result = await query(
      `SELECT id, parent_id, kind, title, content, tasked, task_id, sort_order,
              node_type, node_status, depth, evaluator_score,
              expand_reason, stop_reason, rollup_summary,
              created_at, updated_at
         FROM ideas
         ORDER BY parent_id NULLS FIRST, sort_order, id`,
    );
    return NextResponse.json(result.rows);
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/ideas');
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const {
      parent_id = null,
      kind,
      title,
      content = '',
      node_type = null,
    } = body;

    if (kind !== 'folder' && kind !== 'idea') {
      return NextResponse.json({ error: "kind must be 'folder' or 'idea'" }, { status: 400 });
    }
    if (!title || !title.trim()) {
      return NextResponse.json({ error: 'title is required' }, { status: 400 });
    }
    if (node_type !== null && !['goal', 'subtask', 'leaf'].includes(node_type)) {
      return NextResponse.json(
        { error: "node_type must be 'goal', 'subtask', 'leaf', or null" },
        { status: 400 },
      );
    }

    const result = await query(
      `INSERT INTO ideas (parent_id, kind, title, content, node_type)
       VALUES ($1, $2, $3, $4, $5)
       RETURNING *`,
      [parent_id, kind, title.trim(), kind === 'idea' ? content : '', node_type],
    );
    return NextResponse.json(result.rows[0], { status: 201 });
  } catch (err) {
    return apiErrorResponse(err, 'POST /api/ideas');
  }
}
