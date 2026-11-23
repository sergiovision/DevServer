/**
 * Writes the `injection_sanitised` task event produced by `injection-ingress.ts`.
 *
 * Separate from the pure scan so modules that are unit-tested (webhook firing,
 * the scan itself) never load the Postgres pool. The event rides the usual
 * `task_events` → NOTIFY → WebSocket path, so it shows up in the task timeline
 * with no extra plumbing.
 */

import { query } from './db';
import { INJECTION_EVENT_TYPE, type InjectionSanitisedPayload } from './injection-ingress';

/**
 * Record the findings for a freshly inserted task. Best-effort: the task is
 * already stored and the text already sanitised, so an audit-write failure is
 * logged rather than failing the request.
 */
export async function recordInjectionEvent(
  taskId: number,
  payload: InjectionSanitisedPayload | null,
): Promise<void> {
  if (!payload) return;
  try {
    await query(
      `INSERT INTO task_events (task_id, event_type, payload) VALUES ($1, $2, CAST($3 AS JSONB))`,
      [taskId, INJECTION_EVENT_TYPE, JSON.stringify(payload)],
    );
  } catch (err) {
    console.error(`[injection] failed to record ${INJECTION_EVENT_TYPE} for task ${taskId}:`, err);
  }
}
