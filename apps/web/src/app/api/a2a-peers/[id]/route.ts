import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import { invalidId, jsonError, parseRouteId, type RouteContext } from '@/lib/route-utils';
import { PEER_EDITABLE_FIELDS, validatePeerFields } from '@/lib/a2a/peer-validation';

/**
 * Per-peer admin operations.
 *
 *   PATCH  /api/a2a-peers/<id>   → update scope / ceilings / enabled
 *   DELETE /api/a2a-peers/<id>   → revoke the peer entirely
 *
 * The credential itself is deliberately NOT patchable — rotating a token means
 * delete + recreate, so there is exactly one way a token comes into existence
 * and exactly one moment it is visible.
 */

export async function PATCH(request: NextRequest, context: RouteContext) {
  try {
    const { id } = await context.params;
    const peerId = parseRouteId(id);
    if (peerId === null) return invalidId();

    // Validated by the shared peer validator (lib/a2a/peer-validation.ts) so a
    // malformed field is a 400 with a reason, not a database error. `slug` is
    // not editable: it is embedded in tasks.created_by for every task the peer
    // has already created, so changing it would orphan that history.
    const checked = validatePeerFields(await request.json().catch(() => null));
    if (!checked.ok) {
      return jsonError(400, checked.error);
    }
    const body = checked.values;

    const sets: string[] = [];
    const values: unknown[] = [];
    let paramIndex = 1;
    for (const key of PEER_EDITABLE_FIELDS) {
      if (body[key] !== undefined) {
        sets.push(`${key} = $${paramIndex++}`);
        values.push(body[key]);
      }
    }
    if (sets.length === 0) {
      return jsonError(400, 'No valid fields to update');
    }
    sets.push('updated_at = NOW()');
    values.push(peerId);

    const res = await query(
      `UPDATE a2a_peers SET ${sets.join(', ')} WHERE id = $${paramIndex}
       RETURNING id, name, slug, allowed_repo_ids, allowed_task_types,
                 require_plan_approval, force_git_flow, max_cost_usd,
                 max_wall_seconds, default_agent_vendor, default_model,
                 task_key_prefix, priority, daily_task_quota, tasks_today,
                 quota_reset_on, enabled, last_seen_at, call_count, task_count,
                 created_at`,
      values,
    );
    if (res.rows.length === 0) {
      return jsonError(404, 'peer not found');
    }
    return NextResponse.json({ peer: res.rows[0] });
  } catch (err) {
    return apiErrorResponse(err, 'PATCH /api/a2a-peers/[id]');
  }
}

export async function DELETE(_request: NextRequest, context: RouteContext) {
  try {
    const { id } = await context.params;
    const peerId = parseRouteId(id);
    if (peerId === null) return invalidId();
    // Tasks the peer created are intentionally left in place — they are real
    // work with real history. Only the credential is revoked.
    const res = await query(`DELETE FROM a2a_peers WHERE id = $1 RETURNING id`, [peerId]);
    if (res.rows.length === 0) {
      return jsonError(404, 'peer not found');
    }
    return NextResponse.json({ success: true });
  } catch (err) {
    return apiErrorResponse(err, 'DELETE /api/a2a-peers/[id]');
  }
}
