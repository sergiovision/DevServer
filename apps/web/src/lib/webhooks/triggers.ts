/**
 * Admin data access for `webhook_triggers` (list / create / update / delete).
 *
 * Secrets are write-only: no function here ever returns one. The browser sees
 * `has_secret` instead, which is all the admin page needs to display.
 */

import { randomBytes } from 'node:crypto';
import { query } from '../db';
import type { TriggerFields } from './trigger-input';

/** A trigger as shown in the admin UI — everything except the secret. */
export interface PublicWebhookTrigger {
  id: number;
  token: string;
  name: string;
  repo_id: number;
  repo_name: string | null;
  template_id: number | null;
  has_secret: boolean;
  title_template: string | null;
  description_template: string | null;
  task_key_prefix: string | null;
  priority: number;
  enabled: boolean;
  last_fired_at: string | null;
  fire_count: number;
  created_at: string;
  updated_at: string;
}

/** Bytes of randomness in a trigger URL token (hex-encoded: 48 characters). */
const TOKEN_BYTES = 24;

const PUBLIC_COLUMNS = `w.id, w.token, w.name, w.repo_id, r.name AS repo_name, w.template_id,
  (w.secret IS NOT NULL AND w.secret <> '') AS has_secret,
  w.title_template, w.description_template, w.task_key_prefix, w.priority, w.enabled,
  w.last_fired_at, w.fire_count, w.created_at, w.updated_at`;

async function findPublicTrigger(id: number): Promise<PublicWebhookTrigger | null> {
  const result = await query<PublicWebhookTrigger>(
    `SELECT ${PUBLIC_COLUMNS}
       FROM webhook_triggers w LEFT JOIN repos r ON r.id = w.repo_id
      WHERE w.id = $1`,
    [id],
  );
  return result.rows[0] ?? null;
}

export async function listTriggers(): Promise<PublicWebhookTrigger[]> {
  const result = await query<PublicWebhookTrigger>(
    `SELECT ${PUBLIC_COLUMNS}
       FROM webhook_triggers w LEFT JOIN repos r ON r.id = w.repo_id
      ORDER BY w.created_at DESC`,
  );
  return result.rows;
}

/** Insert a trigger with a freshly generated URL token. */
export async function createTrigger(fields: Partial<TriggerFields>): Promise<PublicWebhookTrigger> {
  const token = randomBytes(TOKEN_BYTES).toString('hex');
  const columns = ['token', ...Object.keys(fields)];
  const values = [token, ...Object.values(fields)];
  const placeholders = values.map((_, index) => `$${index + 1}`);

  const inserted = await query<{ id: number }>(
    `INSERT INTO webhook_triggers (${columns.join(', ')})
     VALUES (${placeholders.join(', ')})
     RETURNING id`,
    values,
  );
  return (await findPublicTrigger(inserted.rows[0].id)) as PublicWebhookTrigger;
}

/** Apply a validated partial update; null when the trigger does not exist. */
export async function updateTrigger(
  id: number,
  fields: Partial<TriggerFields>,
): Promise<PublicWebhookTrigger | null> {
  // Column names come from the validated TriggerFields keys, never from raw input.
  const assignments = Object.keys(fields).map((column, index) => `${column} = $${index + 2}`);
  const result = await query(
    `UPDATE webhook_triggers SET ${assignments.join(', ')}, updated_at = NOW() WHERE id = $1`,
    [id, ...Object.values(fields)],
  );
  if (result.rowCount === 0) return null;
  return findPublicTrigger(id);
}

/** Delete a trigger; false when it did not exist. */
export async function deleteTrigger(id: number): Promise<boolean> {
  const result = await query('DELETE FROM webhook_triggers WHERE id = $1', [id]);
  return (result.rowCount ?? 0) > 0;
}
