import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    const result = await query(
      `SELECT id, parent_id, kind, title, content, tasked, task_id, sort_order,
              created_at, updated_at
         FROM ideas
         ORDER BY parent_id NULLS FIRST, sort_order, id`,
    );
    return NextResponse.json(result.rows);
  } catch (err) {
    console.error('GET /api/ideas error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { parent_id = null, kind, title, content = '' } = body;

    if (kind !== 'folder' && kind !== 'idea') {
      return NextResponse.json({ error: "kind must be 'folder' or 'idea'" }, { status: 400 });
    }
    if (!title || !title.trim()) {
      return NextResponse.json({ error: 'title is required' }, { status: 400 });
    }

    const result = await query(
      `INSERT INTO ideas (parent_id, kind, title, content)
       VALUES ($1, $2, $3, $4)
       RETURNING *`,
      [parent_id, kind, title.trim(), kind === 'idea' ? content : ''],
    );
    return NextResponse.json(result.rows[0], { status: 201 });
  } catch (err) {
    console.error('POST /api/ideas error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
