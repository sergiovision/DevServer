import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const status = searchParams.get('status');
  const repoId = searchParams.get('repo_id');
  const priority = searchParams.get('priority');
  const limit = parseInt(searchParams.get('limit') || '100');
  const offset = parseInt(searchParams.get('offset') || '0');

  let sql = `
    SELECT t.*, r.name as repo_name FROM tasks t
    LEFT JOIN repos r ON r.id = t.repo_id
    WHERE 1=1
  `;
  const params: unknown[] = [];
  let paramIndex = 1;

  if (status) {
    sql += ` AND t.status = $${paramIndex++}`;
    params.push(status);
  }
  if (repoId) {
    sql += ` AND t.repo_id = $${paramIndex++}`;
    params.push(parseInt(repoId));
  }
  if (priority) {
    sql += ` AND t.priority = $${paramIndex++}`;
    params.push(parseInt(priority));
  }

  sql += ` ORDER BY t.priority ASC, t.created_at DESC LIMIT $${paramIndex++} OFFSET $${paramIndex++}`;
  params.push(limit, offset);

  try {
    const result = await query(sql, params);
    return NextResponse.json(result.rows);
  } catch (err) {
    console.error('GET /api/tasks error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const {
      repo_id, task_key, title, description, acceptance,
      priority = 3, labels = [], mode = 'autonomous', claude_mode = 'max',
      agent_vendor = 'anthropic',
      claude_model = null, max_turns = null, skip_verify = false,
      git_flow = 'branch',
    } = body;

    const ALLOWED_VENDORS = ['anthropic', 'google', 'openai', 'glm'];
    if (!ALLOWED_VENDORS.includes(agent_vendor)) {
      return NextResponse.json(
        { error: `agent_vendor must be one of: ${ALLOWED_VENDORS.join(', ')}` },
        { status: 400 },
      );
    }

    if (max_turns !== null && (!Number.isInteger(max_turns) || max_turns <= 0)) {
      return NextResponse.json({ error: 'max_turns must be a positive integer or null' }, { status: 400 });
    }

    if (!repo_id || !task_key || !title) {
      return NextResponse.json(
        { error: 'repo_id, task_key, and title are required' },
        { status: 400 },
      );
    }

    if (/\s/.test(task_key)) {
      return NextResponse.json(
        { error: 'task_key must not contain spaces (use hyphens instead, e.g. MY-TASK)' },
        { status: 400 },
      );
    }

    const result = await query(
      `INSERT INTO tasks (repo_id, task_key, title, description, acceptance, priority, labels, mode, claude_mode, agent_vendor, claude_model, max_turns, skip_verify, git_flow, status)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, 'pending')
       RETURNING *`,
      [repo_id, task_key, title, description || null, acceptance || null, priority, labels, mode, claude_mode, agent_vendor, claude_model || null, max_turns, skip_verify, git_flow],
    );

    return NextResponse.json(result.rows[0], { status: 201 });
  } catch (err) {
    console.error('POST /api/tasks error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
