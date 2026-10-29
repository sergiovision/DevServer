/**
 * Validation of webhook-trigger create / update requests.
 *
 * Pure (no database), so the admin routes stay short and the rules are in one
 * readable place. A create must supply `name`, `repo_id` and `secret`; an
 * update may supply any subset of the editable fields.
 */

/** Editable columns of `webhook_triggers`, keyed by column name. */
export interface TriggerFields {
  name: string;
  repo_id: number;
  template_id: number | null;
  secret: string;
  title_template: string;
  description_template: string;
  task_key_prefix: string;
  priority: number;
  enabled: boolean;
}

export type TriggerInput = { ok: true; values: Partial<TriggerFields> } | { ok: false; error: string };

/** Result of checking a single field. */
type FieldResult<T> = { valid: true; value: T } | { valid: false; error: string };

const MAX_NAME_LENGTH = 128;
const MAX_SECRET_LENGTH = 128;
const KEY_PREFIX_PATTERN = /^[A-Z0-9_-]{1,16}$/;
const PRIORITIES = [1, 2, 3, 4];

function valid<T>(value: T): FieldResult<T> {
  return { valid: true, value };
}

function invalid<T>(error: string): FieldResult<T> {
  return { valid: false, error };
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0;
}

function parseName(value: unknown): FieldResult<string> {
  const name = typeof value === 'string' ? value.trim() : '';
  if (!name) return invalid('name is required');
  if (name.length > MAX_NAME_LENGTH) return invalid(`name must be at most ${MAX_NAME_LENGTH} characters`);
  return valid(name);
}

function parseRepoId(value: unknown): FieldResult<number> {
  return isPositiveInteger(value) ? valid(value) : invalid('repo_id must be a repository id');
}

function parseTemplateId(value: unknown): FieldResult<number | null> {
  if (value === null || value === undefined || value === '') return valid(null);
  return isPositiveInteger(value) ? valid(value) : invalid('template_id must be a template id or null');
}

function parseSecret(value: unknown): FieldResult<string> {
  const secret = typeof value === 'string' ? value.trim() : '';
  if (!secret) return invalid('secret is required — every call must be signed with it');
  if (secret.length > MAX_SECRET_LENGTH) {
    return invalid(`secret must be at most ${MAX_SECRET_LENGTH} characters`);
  }
  return valid(secret);
}

function parseText(field: string) {
  return (value: unknown): FieldResult<string> =>
    typeof value === 'string' ? valid(value) : invalid(`${field} must be a string`);
}

function parseKeyPrefix(value: unknown): FieldResult<string> {
  const prefix = typeof value === 'string' ? value.trim().toUpperCase() : '';
  return KEY_PREFIX_PATTERN.test(prefix)
    ? valid(prefix)
    : invalid('task_key_prefix must be 1–16 characters of A–Z, 0–9, "_" or "-"');
}

function parsePriority(value: unknown): FieldResult<number> {
  return typeof value === 'number' && PRIORITIES.includes(value)
    ? valid(value)
    : invalid('priority must be 1, 2, 3 or 4');
}

function parseEnabled(value: unknown): FieldResult<boolean> {
  return typeof value === 'boolean' ? valid(value) : invalid('enabled must be true or false');
}

const FIELD_PARSERS: { [K in keyof TriggerFields]: (value: unknown) => FieldResult<TriggerFields[K]> } = {
  name: parseName,
  repo_id: parseRepoId,
  template_id: parseTemplateId,
  secret: parseSecret,
  title_template: parseText('title_template'),
  description_template: parseText('description_template'),
  task_key_prefix: parseKeyPrefix,
  priority: parsePriority,
  enabled: parseEnabled,
};

const REQUIRED_ON_CREATE: (keyof TriggerFields)[] = ['name', 'repo_id', 'secret'];

/** Parse every editable field present in `body`; unknown keys are ignored. */
function parseFields(body: unknown): TriggerInput {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { ok: false, error: 'Request body must be a JSON object' };
  }
  const input = body as Record<string, unknown>;
  const values: Record<string, unknown> = {};

  for (const field of Object.keys(FIELD_PARSERS) as (keyof TriggerFields)[]) {
    if (!(field in input)) continue;
    const result = FIELD_PARSERS[field](input[field]);
    if (!result.valid) return { ok: false, error: result.error };
    values[field] = result.value;
  }
  return { ok: true, values: values as Partial<TriggerFields> };
}

export function parseTriggerCreate(body: unknown): TriggerInput {
  const parsed = parseFields(body);
  if (!parsed.ok) return parsed;
  const missing = REQUIRED_ON_CREATE.find((field) => parsed.values[field] === undefined);
  if (missing) {
    return { ok: false, error: `${missing} is required` };
  }
  return parsed;
}

export function parseTriggerUpdate(body: unknown): TriggerInput {
  const parsed = parseFields(body);
  if (!parsed.ok) return parsed;
  if (Object.keys(parsed.values).length === 0) {
    return { ok: false, error: 'No editable fields in request' };
  }
  return parsed;
}
