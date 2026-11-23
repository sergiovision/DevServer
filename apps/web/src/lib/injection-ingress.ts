/**
 * Ingress sanitising for task text — the step every task-creating entry point
 * (task form, webhooks, A2A) runs before writing the row.
 *
 * Pure: it returns the sanitised fields plus the `injection_sanitised` event
 * payload to record (or `null`), and leaves the write to the caller via
 * `recordInjectionEvent` in `injection-ingress-db.ts`. Keeping the database
 * out of this module is what lets `fire.ts` and the tests use it directly.
 *
 * Never refuses: only invisible characters are removed (see
 * `injection-scan.ts`); phrases and comment imperatives are recorded so the
 * operator sees them in the task timeline.
 */

import { scanText, type InjectionFinding } from './injection-scan';

/** Where the task came from — recorded in the event so the timeline says so. */
export type IngressSource = 'api' | 'webhook' | 'a2a' | 'import';

/** Payload of an `injection_sanitised` task event (same shape as the worker's `FieldsScan.to_payload`, plus `source`). */
export interface InjectionSanitisedPayload {
  source: IngressSource;
  removed_invisible: number;
  fields: Record<string, InjectionFinding[]>;
}

export const INJECTION_EVENT_TYPE = 'injection_sanitised';

/** A record of optional text fields; `null`/`undefined`/`''` pass through untouched. */
type TextFields = Record<string, string | null | undefined>;

export interface IngressScan<T extends TextFields> {
  fields: T;
  /** `null` when nothing was found — no event should be written. */
  event: InjectionSanitisedPayload | null;
}

/** Sanitise each text field of a task about to be stored. */
export function sanitiseTaskFields<T extends TextFields>(source: IngressSource, fields: T): IngressScan<T> {
  const sanitised: TextFields = { ...fields };
  const findings: Record<string, InjectionFinding[]> = {};
  let removed = 0;
  for (const [name, value] of Object.entries(fields)) {
    if (!value) continue;
    const result = scanText(value);
    sanitised[name] = result.text;
    removed += result.removedCount;
    if (result.findings.length > 0) findings[name] = result.findings;
  }
  const event =
    Object.keys(findings).length > 0 ? { source, removed_invisible: removed, fields: findings } : null;
  return { fields: sanitised as T, event };
}
