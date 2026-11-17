/**
 * A2A peer authentication + authorization.
 *
 * A peer row is simultaneously the credential and the authorization scope:
 * which repos it may touch, which task types it may create, and the governance
 * ceilings stamped onto every task it spawns.
 *
 * Auth failures are returned as HTTP status codes (401/403/429), not JSON-RPC
 * errors — the A2A spec puts transport authentication at the HTTP layer.
 */

import crypto from 'crypto';
import type { NextRequest } from 'next/server';
import { query } from '@/lib/db';

export interface A2APeer {
  id: number;
  name: string;
  slug: string;
  allowed_repo_ids: number[];
  allowed_task_types: string[];
  require_plan_approval: boolean;
  force_git_flow: string;
  max_cost_usd: string | null;
  max_wall_seconds: number | null;
  default_agent_vendor: string;
  default_model: string | null;
  task_key_prefix: string;
  priority: number;
  daily_task_quota: number;
  tasks_today: number;
  enabled: boolean;
}

export type AuthResult =
  | { ok: true; peer: A2APeer }
  | { ok: false; status: number; error: string };

const PEER_COLUMNS = `id, name, slug, allowed_repo_ids, allowed_task_types,
        require_plan_approval, force_git_flow, max_cost_usd, max_wall_seconds,
        default_agent_vendor, default_model, task_key_prefix, priority,
        daily_task_quota, tasks_today, enabled`;

export function hashToken(token: string): string {
  return crypto.createHash('sha256').update(token, 'utf8').digest('hex');
}

/** Generate a new peer token. 32 bytes → 43 chars base64url. */
export function generateToken(): string {
  return crypto.randomBytes(32).toString('base64url');
}

/**
 * Resolve the caller from the ``Authorization: Bearer`` header.
 *
 * The lookup is a single indexed query on the SHA-256 of the presented token.
 * No constant-time compare is needed (unlike the webhook HMAC path, which
 * compares against a value derived from attacker-controlled input): the index
 * probe is on a uniformly-distributed hash, so response timing carries no
 * information about the token's plaintext prefix.
 */
export async function authenticatePeer(request: NextRequest): Promise<AuthResult> {
  const header = request.headers.get('authorization') || '';
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  if (!match) {
    return { ok: false, status: 401, error: 'missing bearer token' };
  }
  const token = match[1].trim();
  // Cheap shape gate so a garbage header never reaches the database.
  if (token.length < 20 || token.length > 512) {
    return { ok: false, status: 401, error: 'invalid token' };
  }

  const res = await query<A2APeer>(
    `SELECT ${PEER_COLUMNS} FROM a2a_peers WHERE token_sha256 = $1`,
    [hashToken(token)],
  );
  if (res.rows.length === 0) {
    return { ok: false, status: 401, error: 'invalid token' };
  }
  const peer = res.rows[0];
  if (!peer.enabled) {
    return { ok: false, status: 403, error: 'peer disabled' };
  }

  // Liveness bookkeeping. Deliberately not awaited into the critical path's
  // error handling — a failed counter update must never fail a valid request.
  void query(
    `UPDATE a2a_peers SET last_seen_at = NOW(), call_count = call_count + 1 WHERE id = $1`,
    [peer.id],
  ).catch(() => {});

  return { ok: true, peer };
}

/**
 * Atomically consume one unit of the peer's daily task quota.
 *
 * The reset and the decrement happen in one statement so two concurrent
 * ``message/send`` calls cannot both pass a check-then-act race. Returns false
 * when the peer is out of budget for the day.
 */
export async function consumeTaskQuota(peerId: number): Promise<boolean> {
  const res = await query<{ tasks_today: number }>(
    `UPDATE a2a_peers
        SET tasks_today = CASE WHEN quota_reset_on < CURRENT_DATE THEN 1
                               ELSE tasks_today + 1 END,
            quota_reset_on = CURRENT_DATE,
            task_count = task_count + 1,
            updated_at = NOW()
      WHERE id = $1
        AND (quota_reset_on < CURRENT_DATE OR tasks_today < daily_task_quota)
      RETURNING tasks_today`,
    [peerId],
  );
  return res.rows.length > 0;
}

export function peerMayUseRepo(peer: A2APeer, repoId: number | null): boolean {
  if (repoId === null) return true; // repo-less task types (skill/research)
  return (peer.allowed_repo_ids || []).includes(repoId);
}

export function peerMayUseTaskType(peer: A2APeer, taskType: string): boolean {
  return (peer.allowed_task_types || []).includes(taskType);
}

/**
 * Audit tag written to ``tasks.created_by`` for every task a peer creates, and
 * the tenancy key every peer-scoped read filters on. The slug carries a random
 * per-peer suffix (``makePeerSlug``), so a recreated peer never inherits a
 * deleted peer's tasks.
 */
export function peerCreatedBy(peer: A2APeer): string {
  return `a2a:${peer.slug}`;
}
