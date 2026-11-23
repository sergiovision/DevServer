/**
 * Post-completion action — the follow-up a task fires when it finishes.
 *
 * Stored in `tasks.post_completion_action` / `task_templates.post_completion_action`
 * (JSONB, migration 004) and executed by the worker
 * (`apps/worker/src/services/post_completion.py`): re-run another task, or start a
 * new `skill` task in the same repo, on success, on failure or always.
 *
 * This module is the single validator every write path uses (task create/PATCH,
 * template create/PATCH), so a malformed value never reaches the column. It is
 * pure — no DB, no DOM — and returns a result instead of throwing so routes can
 * map it straight to a 400.
 *
 * Invariants:
 * - `null` clears the action; it is a valid value, not an error.
 * - There is deliberately no `script` kind: running arbitrary commands on
 *   completion is a different (and much riskier) feature.
 * - Unknown keys are dropped, so the stored JSON has exactly the documented shape.
 */

export const POST_COMPLETION_KINDS = ['task', 'skill'] as const;
export type PostCompletionKind = (typeof POST_COMPLETION_KINDS)[number];

export const POST_COMPLETION_TRIGGERS = ['on_success', 'on_failure', 'always'] as const;
export type PostCompletionTrigger = (typeof POST_COMPLETION_TRIGGERS)[number];

export const POST_COMPLETION_TRIGGER_LABELS: Record<PostCompletionTrigger, string> = {
  on_success: 'On success',
  on_failure: 'On failure',
  always: 'Always',
};

export type PostCompletionAction =
  | { kind: 'task'; task_id: number; trigger: PostCompletionTrigger }
  | { kind: 'skill'; skill_name: string; trigger: PostCompletionTrigger };

export type ParseResult =
  | { ok: true; value: PostCompletionAction | null }
  | { ok: false; error: string };

/** Longest skill name accepted; skills are folder names, so this is generous. */
const MAX_SKILL_NAME_LENGTH = 128;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isTrigger(value: unknown): value is PostCompletionTrigger {
  return typeof value === 'string' && (POST_COMPLETION_TRIGGERS as readonly string[]).includes(value);
}

/**
 * Validate an untrusted value (request body field, template row) as a
 * post-completion action. `null` / `undefined` mean "no action".
 */
export function parsePostCompletionAction(input: unknown): ParseResult {
  if (input === null || input === undefined) return { ok: true, value: null };
  if (!isRecord(input)) {
    return { ok: false, error: 'post_completion_action must be an object or null' };
  }

  const { kind, trigger } = input;
  if (!isTrigger(trigger)) {
    return {
      ok: false,
      error: `post_completion_action.trigger must be one of: ${POST_COMPLETION_TRIGGERS.join(', ')}`,
    };
  }

  if (kind === 'task') {
    const taskId = input.task_id;
    if (typeof taskId !== 'number' || !Number.isInteger(taskId) || taskId <= 0) {
      return { ok: false, error: 'post_completion_action.task_id must be a positive integer' };
    }
    return { ok: true, value: { kind: 'task', task_id: taskId, trigger } };
  }

  if (kind === 'skill') {
    const skillName = typeof input.skill_name === 'string' ? input.skill_name.trim() : '';
    if (!skillName) {
      return { ok: false, error: 'post_completion_action.skill_name is required' };
    }
    if (skillName.length > MAX_SKILL_NAME_LENGTH) {
      return { ok: false, error: `post_completion_action.skill_name is longer than ${MAX_SKILL_NAME_LENGTH} characters` };
    }
    return { ok: true, value: { kind: 'skill', skill_name: skillName, trigger } };
  }

  return {
    ok: false,
    error: `post_completion_action.kind must be one of: ${POST_COMPLETION_KINDS.join(', ')}`,
  };
}

/**
 * Extra check for a task's own action: a task may not re-run itself. The
 * worker refuses it too; rejecting it on write gives the operator the reason
 * immediately instead of on the event log after a run.
 */
export function selfReferenceError(action: PostCompletionAction | null, taskId: number): string | null {
  if (action?.kind === 'task' && action.task_id === taskId) {
    return 'post_completion_action cannot target the task itself';
  }
  return null;
}

/** The SQL parameter for a validated value: JSON text, or null to clear. */
export function toJsonbParam(action: PostCompletionAction | null): string | null {
  return action === null ? null : JSON.stringify(action);
}

/**
 * Editable form state. A half-filled form (kind chosen, no target yet) is not a
 * valid action, so the picker edits a draft and the caller converts it on save.
 */
export interface PostCompletionDraft {
  kind: 'none' | PostCompletionKind;
  /** Kept as text so an empty input stays empty instead of becoming 0. */
  taskId: string;
  skillName: string;
  trigger: PostCompletionTrigger;
}

export const EMPTY_POST_COMPLETION_DRAFT: PostCompletionDraft = {
  kind: 'none',
  taskId: '',
  skillName: '',
  trigger: 'on_success',
};

/** Draft for a stored value; anything malformed starts as "none". */
export function actionToDraft(stored: unknown): PostCompletionDraft {
  const parsed = parsePostCompletionAction(stored);
  if (!parsed.ok || parsed.value === null) return EMPTY_POST_COMPLETION_DRAFT;
  const action = parsed.value;
  return action.kind === 'task'
    ? { kind: 'task', taskId: String(action.task_id), skillName: '', trigger: action.trigger }
    : { kind: 'skill', taskId: '', skillName: action.skill_name, trigger: action.trigger };
}

/** Validate a draft: `none` clears, anything else must be a complete action. */
export function draftToAction(draft: PostCompletionDraft): ParseResult {
  if (draft.kind === 'none') return { ok: true, value: null };
  if (draft.kind === 'task') {
    const text = draft.taskId.trim();
    const taskId = /^\d+$/.test(text) ? Number(text) : Number.NaN;
    return parsePostCompletionAction({ kind: 'task', task_id: taskId, trigger: draft.trigger });
  }
  return parsePostCompletionAction({ kind: 'skill', skill_name: draft.skillName, trigger: draft.trigger });
}

/** One-line description for read-only displays ("On success → run task #12"). */
export function describePostCompletionAction(action: PostCompletionAction | null): string {
  if (action === null) return 'None';
  const when = POST_COMPLETION_TRIGGER_LABELS[action.trigger];
  return action.kind === 'task'
    ? `${when} → run task #${action.task_id}`
    : `${when} → run skill ${action.skill_name}`;
}
