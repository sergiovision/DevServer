import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

const UPDATABLE = new Set([
  'parent_id',
  'title',
  'content',
  'tasked',
  'task_id',
  'sort_order',
]);

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await params;
    const ideaId = parseInt(id, 10);
    if (!Number.isInteger(ideaId)) {
      return NextResponse.json({ error: 'Invalid id' }, { status: 400 });
    }

    const body = await request.json();
    const fields: string[] = [];
    const values: unknown[] = [];
    let i = 1;
    for (const [key, value] of Object.entries(body)) {
      if (!UPDATABLE.has(key)) continue;
      fields.push(`${key} = $${i++}`);
      values.push(value);
    }

    if (fields.length === 0) {
      return NextResponse.json({ error: 'No updatable fields provided' }, { status: 400 });
    }

    values.push(ideaId);
    const sql = `UPDATE ideas SET ${fields.join(', ')} WHERE id = $${i} RETURNING *`;
    const result = await query(sql, values);
    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Not found' }, { status: 404 });
    }
    return NextResponse.json(result.rows[0]);
  } catch (err) {
    console.error('PATCH /api/ideas/[id] error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export async function DELETE(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await params;
    const ideaId = parseInt(id, 10);
    if (!Number.isInteger(ideaId)) {
      return NextResponse.json({ error: 'Invalid id' }, { status: 400 });
    }
    const result = await query('DELETE FROM ideas WHERE id = $1 RETURNING id', [ideaId]);
    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Not found' }, { status: 404 });
    }
    return NextResponse.json({ ok: true });
  } catch (err) {
    console.error('DELETE /api/ideas/[id] error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
