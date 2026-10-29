/**
 * Firing a webhook trigger: turn an authenticated HTTP call into a queued task.
 *
 * The route handler stays a thin adapter (read the raw body, call this, map
 * the outcome to a status code). Everything that touches the database goes
 * through the `WebhookStore` interface, so this function can be tested with
 * an in-memory fake — see `fire.test.ts`. The real store is in `store.ts`.
 */

import { randomBytes } from 'node:crypto';
import { renderTemplate } from './template';
import { verifySignature } from './signature';

/** A row of `webhook_triggers`, as much of it as firing needs. */
export interface WebhookTrigger {
  id: number;
  name: string;
  repo_id: number;
  template_id: number | null;
  secret: string | null;
  title_template: string | null;
  description_template: string | null;
  task_key_prefix: string | null;
  priority: number;
  enabled: boolean;
}

/** The task a firing should create, already rendered. */
export interface WebhookTaskRequest {
  trigger: WebhookTrigger;
  taskKey: string;
  title: string;
  description: string;
}

export interface FiredTask {
  taskId: number;
  taskKey: string;
}

/** Data access used while firing — implemented against Postgres in `store.ts`. */
export interface WebhookStore {
  findTriggerByToken(token: string): Promise<WebhookTrigger | null>;
  /** Insert the task (applying the trigger's template, if any) and enqueue it. */
  createAndEnqueueTask(request: WebhookTaskRequest): Promise<FiredTask>;
  /** Bump the trigger's fire counters and log a `webhook_triggered` task event. */
  recordFiring(trigger: WebhookTrigger, task: FiredTask): Promise<void>;
}

export interface IncomingWebhook {
  token: string;
  /** The body exactly as received — the signature is computed over these bytes. */
  rawBody: string;
  /** Value of the `X-DevServer-Signature` header, if present. */
  signature: string | null;
}

export type FireOutcome =
  | { ok: true; taskId: number; taskKey: string }
  | { ok: false; status: 400 | 401 | 404; error: string };

export interface FireOptions {
  /** Injectable clock and randomness, so task keys are predictable in tests. */
  now?: Date;
  randomSuffix?: () => string;
}

export const SIGNATURE_HEADER = 'X-DevServer-Signature';

const DEFAULT_TITLE_TEMPLATE = 'Webhook: {{name}}';
const DEFAULT_KEY_PREFIX = 'HOOK';
const MAX_KEY_PREFIX_LENGTH = 16;

/** Keep a configured prefix to characters that are safe in a task key. */
function normalizeKeyPrefix(prefix: string | null): string {
  const cleaned = (prefix ?? '')
    .toUpperCase()
    .replace(/[^A-Z0-9_-]/g, '')
    .slice(0, MAX_KEY_PREFIX_LENGTH);
  return cleaned || DEFAULT_KEY_PREFIX;
}

function defaultRandomSuffix(): string {
  return randomBytes(2).toString('hex');
}

/**
 * A task key such as `HOOK-20261029-143005-a1b2`: the trigger's prefix, the
 * UTC time of the call, and a short random suffix so two calls in the same
 * second do not collide.
 */
export function webhookTaskKey(prefix: string | null, now: Date, suffix: string): string {
  const iso = now.toISOString(); // 2026-10-29T14:30:05.123Z
  const date = iso.slice(0, 10).replace(/-/g, '');
  const time = iso.slice(11, 19).replace(/:/g, '');
  return `${normalizeKeyPrefix(prefix)}-${date}-${time}-${suffix}`;
}

function parseJson(rawBody: string): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: JSON.parse(rawBody) };
  } catch {
    return { ok: false };
  }
}

/**
 * Validate an incoming call and, if it is genuine, create and enqueue a task.
 *
 * Checks run in a deliberate order: an unknown or disabled token is a 404
 * (revealing nothing about which tokens exist), the signature is checked
 * before the body is even parsed, and only then is malformed JSON a 400.
 */
export async function fireWebhook(
  store: WebhookStore,
  incoming: IncomingWebhook,
  options: FireOptions = {},
): Promise<FireOutcome> {
  const trigger = await store.findTriggerByToken(incoming.token);
  if (!trigger || !trigger.enabled) {
    return { ok: false, status: 404, error: 'Unknown webhook' };
  }

  // A trigger without a secret cannot authenticate anyone, so it never fires.
  const secret = trigger.secret ?? '';
  if (!verifySignature(incoming.rawBody, incoming.signature, secret)) {
    return { ok: false, status: 401, error: `Missing or invalid ${SIGNATURE_HEADER} header` };
  }

  const parsed = parseJson(incoming.rawBody);
  if (!parsed.ok) {
    return { ok: false, status: 400, error: 'Request body must be valid JSON' };
  }

  const extra = { name: trigger.name };
  const titleTemplate = trigger.title_template?.trim() || DEFAULT_TITLE_TEMPLATE;
  const title = renderTemplate(titleTemplate, parsed.value, extra).trim()
    || `Webhook: ${trigger.name}`;
  const description = renderTemplate(trigger.description_template ?? '', parsed.value, extra);

  const now = options.now ?? new Date();
  const suffix = (options.randomSuffix ?? defaultRandomSuffix)();
  const taskKey = webhookTaskKey(trigger.task_key_prefix, now, suffix);

  const task = await store.createAndEnqueueTask({ trigger, taskKey, title, description });
  await store.recordFiring(trigger, task);
  return { ok: true, taskId: task.taskId, taskKey: task.taskKey };
}
