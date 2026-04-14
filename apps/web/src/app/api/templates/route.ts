import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

export async function GET() {
  try {
    const result = await query(
      'SELECT * FROM task_templates ORDER BY name ASC',
    );
    return NextResponse.json(result.rows);
  } catch (err) {
    console.error('GET /api/templates error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const {
      name,
      description = null,
      acceptance = null,
      git_flow = 'branch',
      claude_mode = 'max',
      agent_vendor = 'anthropic',
      claude_model = null,
      backup_vendor = null,
      backup_model = null,
      max_turns = null,
      skip_verify = false,
    } = body;

    if (!name?.trim()) {
      return NextResponse.json({ error: 'name is required' }, { status: 400 });
    }

    const result = await query(
      `INSERT INTO task_templates (name, description, acceptance, git_flow, claude_mode, agent_vendor, claude_model, backup_vendor, backup_model, max_turns, skip_verify)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
       RETURNING *`,
      [name.trim(), description || null, acceptance || null, git_flow, claude_mode, agent_vendor, claude_model || null, backup_vendor || null, backup_model || null, max_turns, skip_verify],
    );

    return NextResponse.json(result.rows[0], { status: 201 });
  } catch (err) {
    console.error('POST /api/templates error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
