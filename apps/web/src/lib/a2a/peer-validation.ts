/**
 * Validation for the A2A peer admin API (`POST /api/a2a-peers`,
 * `PATCH /api/a2a-peers/[id]`).
 *
 * One validator for both routes so a malformed PATCH is a 400 with a reason,
 * never a database error surfacing as a 500.
 */

import crypto from 'crypto';

export const PEER_TASK_TYPES = ['coding', 'test', 'skill', 'script', 'research'];
export const PEER_GIT_FLOWS = ['branch', 'commit', 'patch', 'untracked'];
export const PEER_VENDORS = ['anthropic', 'google', 'openai', 'glm'];

/** Columns an admin may set. `slug` is absent: it is the immutable audit tag. */
export const PEER_EDITABLE_FIELDS = [
  'name',
  'allowed_repo_ids',
  'allowed_task_types',
  'require_plan_approval',
  'force_git_flow',
  'max_cost_usd',
  'max_wall_seconds',
  'default_agent_vendor',
  'default_model',
  'task_key_prefix',
  'priority',
  'daily_task_quota',
  'enabled',
] as const;

export type PeerField = (typeof PEER_EDITABLE_FIELDS)[number];

export type PeerValidation =
  | { ok: true; values: Partial<Record<PeerField, unknown>> }
  | { ok: false; error: string };

const MAX_INT4 = 2_147_483_647;

/**
 * Validate and normalise the fields present in `body`.
 *
 * Only keys that are present (not `undefined`) are validated and returned, so
 * the same function serves a full create (after defaults are applied) and a
 * partial update.
 */
export function validatePeerFields(body: unknown): PeerValidation {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { ok: false, error: 'request body must be a JSON object' };
  }
  const b = body as Record<string, unknown>;
  const values: Partial<Record<PeerField, unknown>> = {};
  const fail = (error: string): PeerValidation => ({ ok: false, error });

  if (b.name !== undefined) {
    if (typeof b.name !== 'string' || !b.name.trim()) return fail('name is required');
    values.name = b.name.trim().slice(0, 128);
  }
  if (b.allowed_repo_ids !== undefined) {
    if (
      !Array.isArray(b.allowed_repo_ids) ||
      b.allowed_repo_ids.some((r) => !Number.isInteger(r) || r < 1 || r > MAX_INT4)
    ) {
      return fail('allowed_repo_ids must be an array of positive integers');
    }
    values.allowed_repo_ids = b.allowed_repo_ids;
  }
  if (b.allowed_task_types !== undefined) {
    if (
      !Array.isArray(b.allowed_task_types) ||
      b.allowed_task_types.length === 0 ||
      b.allowed_task_types.some((t) => typeof t !== 'string' || !PEER_TASK_TYPES.includes(t))
    ) {
      return fail(
        `allowed_task_types must be a non-empty subset of: ${PEER_TASK_TYPES.join(', ')}`,
      );
    }
    values.allowed_task_types = b.allowed_task_types;
  }
  for (const key of ['require_plan_approval', 'enabled'] as const) {
    if (b[key] !== undefined) {
      if (typeof b[key] !== 'boolean') return fail(`${key} must be a boolean`);
      values[key] = b[key];
    }
  }
  if (b.force_git_flow !== undefined) {
    if (typeof b.force_git_flow !== 'string' || !PEER_GIT_FLOWS.includes(b.force_git_flow)) {
      return fail(`force_git_flow must be one of: ${PEER_GIT_FLOWS.join(', ')}`);
    }
    values.force_git_flow = b.force_git_flow;
  }
  if (b.default_agent_vendor !== undefined) {
    if (
      typeof b.default_agent_vendor !== 'string' ||
      !PEER_VENDORS.includes(b.default_agent_vendor)
    ) {
      return fail(`default_agent_vendor must be one of: ${PEER_VENDORS.join(', ')}`);
    }
    values.default_agent_vendor = b.default_agent_vendor;
  }
  if (b.max_cost_usd !== undefined) {
    const v = b.max_cost_usd;
    if (v !== null && (typeof v !== 'number' || !Number.isFinite(v) || v <= 0 || v >= 1_000_000)) {
      return fail('max_cost_usd must be null or a positive number below 1000000');
    }
    values.max_cost_usd = v;
  }
  if (b.max_wall_seconds !== undefined) {
    const v = b.max_wall_seconds;
    if (v !== null && (!Number.isInteger(v) || (v as number) <= 0 || (v as number) > MAX_INT4)) {
      return fail('max_wall_seconds must be null or a positive integer');
    }
    values.max_wall_seconds = v;
  }
  if (b.default_model !== undefined) {
    const v = b.default_model;
    if (v !== null && (typeof v !== 'string' || v.length > 64)) {
      return fail('default_model must be null or a string of at most 64 characters');
    }
    values.default_model = v || null;
  }
  if (b.task_key_prefix !== undefined) {
    const v = b.task_key_prefix;
    if (typeof v !== 'string' || !/^[A-Za-z0-9_-]{1,16}$/.test(v)) {
      return fail('task_key_prefix must be 1-16 characters of A-Z, a-z, 0-9, _ or -');
    }
    values.task_key_prefix = v;
  }
  if (b.priority !== undefined) {
    if (!Number.isInteger(b.priority) || (b.priority as number) < 1 || (b.priority as number) > 4) {
      return fail('priority must be an integer from 1 (critical) to 4 (low)');
    }
    values.priority = b.priority;
  }
  if (b.daily_task_quota !== undefined) {
    const v = b.daily_task_quota;
    if (!Number.isInteger(v) || (v as number) < 0 || (v as number) > MAX_INT4) {
      return fail('daily_task_quota must be a non-negative integer');
    }
    values.daily_task_quota = v;
  }

  return { ok: true, values };
}

/**
 * Build the immutable per-peer slug used in `tasks.created_by` ('a2a:<slug>').
 *
 * The random suffix makes the tag unique to *this* peer row forever: a peer
 * deleted and recreated under the same name gets a new slug and therefore does
 * NOT inherit the old peer's tasks, and two names that differ only in
 * punctuation ("ci bot" / "ci-bot") no longer collide on the UNIQUE index.
 */
export function makePeerSlug(name: string): string | null {
  const base = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
    .replace(/-+$/, '');
  if (!base) return null;
  return `${base}-${crypto.randomBytes(4).toString('hex')}`;
}

/** True for a Postgres unique-violation error. */
export function isUniqueViolation(err: unknown): boolean {
  return (
    !!err && typeof err === 'object' && (err as { code?: unknown }).code === '23505'
  );
}
