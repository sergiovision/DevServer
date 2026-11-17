import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import { generateToken, hashToken } from '@/lib/a2a/auth';
import {
  isUniqueViolation,
  makePeerSlug,
  validatePeerFields,
} from '@/lib/a2a/peer-validation';

/**
 * Admin CRUD for A2A peers.
 *
 *   GET  /api/a2a-peers   → list peers (never returns tokens)
 *   POST /api/a2a-peers   → create a peer, returning the token ONCE
 *
 * Only the SHA-256 of the token is stored, so the plaintext is unrecoverable
 * after this response. Rotation is delete + recreate — same design decision as
 * webhook triggers, and the reason there is no edit form for the credential.
 */

const LIST_COLUMNS = `id, name, slug, allowed_repo_ids, allowed_task_types,
        require_plan_approval, force_git_flow, max_cost_usd, max_wall_seconds,
        default_agent_vendor, default_model, task_key_prefix, priority,
        daily_task_quota, tasks_today, quota_reset_on, enabled,
        last_seen_at, call_count, task_count, created_at`;

export async function GET() {
  try {
    const res = await query(
      `SELECT ${LIST_COLUMNS} FROM a2a_peers ORDER BY created_at DESC`,
    );
    return NextResponse.json({ peers: res.rows });
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/a2a-peers');
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json().catch(() => null);
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      return NextResponse.json({ error: 'request body must be a JSON object' }, { status: 400 });
    }
    // Defaults are the safest combination; see 003_a2a.sql.
    const checked = validatePeerFields({
      allowed_repo_ids: [],
      allowed_task_types: ['research'],
      require_plan_approval: true,
      force_git_flow: 'patch',
      max_cost_usd: null,
      max_wall_seconds: null,
      default_agent_vendor: 'anthropic',
      default_model: null,
      task_key_prefix: 'A2A',
      priority: 3,
      daily_task_quota: 10,
      ...body,
      name: body.name ?? '',
    });
    if (!checked.ok) {
      return NextResponse.json({ error: checked.error }, { status: 400 });
    }
    const v = checked.values;

    // Slug is the audit tag baked into tasks.created_by ('a2a:<slug>'). It
    // carries a random suffix so it is unique to this peer row forever — see
    // makePeerSlug.
    const slug = makePeerSlug(String(v.name));
    if (!slug) {
      return NextResponse.json(
        { error: 'name must contain at least one alphanumeric character' },
        { status: 400 },
      );
    }

    const token = generateToken();

    const res = await query(
      `INSERT INTO a2a_peers
         (name, slug, token_sha256, allowed_repo_ids, allowed_task_types,
          require_plan_approval, force_git_flow, max_cost_usd, max_wall_seconds,
          default_agent_vendor, default_model, task_key_prefix, priority,
          daily_task_quota)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
       RETURNING ${LIST_COLUMNS}`,
      [
        v.name,
        slug,
        hashToken(token),
        v.allowed_repo_ids,
        v.allowed_task_types,
        v.require_plan_approval,
        v.force_git_flow,
        v.max_cost_usd,
        v.max_wall_seconds,
        v.default_agent_vendor,
        v.default_model,
        v.task_key_prefix,
        v.priority,
        v.daily_task_quota,
      ],
    );

    // The only time the plaintext token is ever returned.
    return NextResponse.json({ peer: res.rows[0], token }, { status: 201 });
  } catch (err) {
    if (isUniqueViolation(err)) {
      return NextResponse.json(
        { error: 'a peer with a conflicting slug or token already exists; retry' },
        { status: 409 },
      );
    }
    return apiErrorResponse(err, 'POST /api/a2a-peers');
  }
}
