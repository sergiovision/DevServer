import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import { insertTask } from '@/lib/tasks';
import { sanitiseTaskFields } from '@/lib/injection-ingress';
import { recordInjectionEvent } from '@/lib/injection-ingress-db';
import { parsePostCompletionAction, toJsonbParam } from '@/lib/post-completion-action';
import type { Task } from '@/lib/types';

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
    return apiErrorResponse(err, 'GET /api/tasks');
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const {
      repo_id, task_key, title, description, acceptance,
      priority = 3, labels = [], mode = 'autonomous', claude_mode = 'max',
      task_type = 'coding',
      agent_vendor = 'anthropic',
      claude_model = null, max_turns = null, skip_verify = false,
      git_flow = 'branch',
      backup_vendor = null,
      backup_model = null,
    } = body;

    // Task type drives execution + which fields apply. 'coding' (default),
    // 'test' and 'script' need a repo; 'skill' and 'research' do not.
    const ALLOWED_TASK_TYPES = ['coding', 'test', 'skill', 'script', 'research'];
    if (!ALLOWED_TASK_TYPES.includes(task_type)) {
      return NextResponse.json(
        { error: `task_type must be one of: ${ALLOWED_TASK_TYPES.join(', ')}` },
        { status: 400 },
      );
    }
    const repoRequired = ['coding', 'test', 'script'].includes(task_type);

    const ALLOWED_VENDORS = ['anthropic', 'google', 'openai', 'glm'];
    if (!ALLOWED_VENDORS.includes(agent_vendor)) {
      return NextResponse.json(
        { error: `agent_vendor must be one of: ${ALLOWED_VENDORS.join(', ')}` },
        { status: 400 },
      );
    }
    if (backup_vendor !== null && !ALLOWED_VENDORS.includes(backup_vendor)) {
      return NextResponse.json(
        { error: `backup_vendor must be one of: ${ALLOWED_VENDORS.join(', ')}` },
        { status: 400 },
      );
    }

    if (max_turns !== null && (!Number.isInteger(max_turns) || max_turns <= 0)) {
      return NextResponse.json({ error: 'max_turns must be a positive integer or null' }, { status: 400 });
    }

    if (!task_key || !title) {
      return NextResponse.json(
        { error: 'task_key and title are required' },
        { status: 400 },
      );
    }
    if (repoRequired && !repo_id) {
      return NextResponse.json(
        { error: `repo_id is required for ${task_type} tasks` },
        { status: 400 },
      );
    }

    if (/\s/.test(task_key)) {
      return NextResponse.json(
        { error: 'task_key must not contain spaces (use hyphens instead, e.g. MY-TASK)' },
        { status: 400 },
      );
    }

    // ── Post-completion action (optional follow-up; lib/post-completion-action.ts) ──
    const postCompletion = parsePostCompletionAction(body.post_completion_action);
    if (!postCompletion.ok) {
      return NextResponse.json({ error: postCompletion.error }, { status: 400 });
    }

    // Untrusted text is cleaned before it is stored (invisible characters
    // removed, override attempts flagged) — never refused.
    const scan = sanitiseTaskFields('api', {
      title: String(title),
      description: typeof description === 'string' ? description : null,
      acceptance: typeof acceptance === 'string' ? acceptance : null,
    });

    const created = await insertTask({
      repoId: repo_id || null,
      taskKey: task_key,
      title: scan.fields.title,
      description: scan.fields.description || null,
      acceptance: scan.fields.acceptance || null,
      priority,
      labels,
      mode,
      taskType: task_type,
      claudeMode: claude_mode,
      agentVendor: agent_vendor,
      claudeModel: claude_model || null,
      maxTurns: max_turns,
      skipVerify: skip_verify,
      gitFlow: git_flow,
      backupVendor: backup_vendor || null,
      backupModel: backup_model || null,
    });

    await recordInjectionEvent(created.id, scan.event);

    // ── Post-completion action: stored after the shared insert (lib/tasks.ts
    // owns the INSERT column list), only when one was supplied.
    if (postCompletion.value !== null) {
      const stored = await query<Pick<Task, 'post_completion_action'>>(
        `UPDATE tasks SET post_completion_action = $1::jsonb WHERE id = $2 RETURNING post_completion_action`,
        [toJsonbParam(postCompletion.value), created.id],
      );
      created.post_completion_action = stored.rows[0]?.post_completion_action ?? null;
    }

    return NextResponse.json(created, { status: 201 });
  } catch (err) {
    return apiErrorResponse(err, 'POST /api/tasks');
  }
}
