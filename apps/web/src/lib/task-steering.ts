/**
 * Task steering — the two ways a *human or an external agent* redirects a task
 * that is already in flight:
 *
 *   1. `decidePlan`          — approve/reject an interactive task's plan gate.
 *   2. `sendOperatorMessage` — drop a message into the task's inbox and, when
 *                              the task is idle, kick it so it actually reads it.
 *
 * Both used to live inline in the dashboard's route handlers. They were pulled
 * out here when the A2A gateway needed the identical behaviour: an external
 * orchestrator resolving an `input-required` task must go through exactly the
 * same state transitions as the operator clicking the button, or the two
 * surfaces drift and only one of them stays correct.
 *
 * Callers get a discriminated union rather than an HTTP response, so the
 * dashboard can map it to a status code and the gateway to a JSON-RPC error.
 */

import { query } from './db';
import { enqueueTask } from './queue';
import { WORKER_URL } from './worker-url';

/**
 * Idle states that still hold a live agent branch / session_id, so a follow-up
 * message can resume the same task. `running`/`verifying` are excluded — the
 * agent is live and drains the inbox between steps, so re-enqueueing would
 * needlessly interrupt it. `pending`/`queued` are already on their way to run.
 * `done`/`retired` are terminal (the worker's continue endpoint refuses them).
 */
export const CONTINUABLE_IDLE = new Set(['test', 'failed', 'blocked', 'cancelled']);

export type SteerFailure = { ok: false; status: number; error: string };
export type PlanDecision =
  | { ok: true; action: 'approve' | 'reject' }
  | SteerFailure;
export type MessageDelivery =
  | { ok: true; triggered: boolean; data: Record<string, unknown> }
  | SteerFailure;

interface SteerTaskRow {
  id: number;
  task_key: string;
  status: string;
  repo_id: number | null;
  title: string;
  priority: number;
  mode: string;
  claude_mode: string;
  max_turns: number | null;
  git_flow: string | null;
}

const TASK_COLUMNS = `id, task_key, status, repo_id, title, priority, mode,
        claude_mode, max_turns, git_flow`;

/**
 * Approve or reject an interactive task's plan.
 *
 * The worker's `plan_gate.wait_for_approval` busy-polls `plan_approved_at` /
 * `plan_rejected_at` every 5s, so setting the column is the entire handoff —
 * there is nothing to notify.
 */
export async function decidePlan(
  taskId: number,
  action: 'approve' | 'reject',
): Promise<PlanDecision> {
  const res = await query<{
    id: number;
    mode: string;
    plan_approved_at: string | null;
    plan_rejected_at: string | null;
  }>(
    `SELECT id, mode, plan_approved_at, plan_rejected_at FROM tasks WHERE id = $1`,
    [taskId],
  );
  if (res.rows.length === 0) {
    return { ok: false, status: 404, error: 'Task not found' };
  }
  const task = res.rows[0];

  if (task.mode !== 'interactive') {
    return {
      ok: false,
      status: 400,
      error: `Task ${taskId} is not in interactive mode (mode=${task.mode})`,
    };
  }
  if (task.plan_approved_at || task.plan_rejected_at) {
    return {
      ok: false,
      status: 409,
      error: `Plan already ${task.plan_approved_at ? 'approved' : 'rejected'}`,
    };
  }

  const column = action === 'approve' ? 'plan_approved_at' : 'plan_rejected_at';
  await query(
    `UPDATE tasks SET ${column} = NOW(), updated_at = NOW() WHERE id = $1`,
    [taskId],
  );
  return { ok: true, action };
}

/**
 * Deliver a message into a task's inbox from the reserved `operator` key, then
 * resume the task if it is idle.
 *
 * The kick matters: agents drain the operator inbox only at the *start* of a
 * run, so without it a message to an idle task sits unread and the sender sees
 * nothing happen.
 */
export async function sendOperatorMessage(
  taskId: number,
  msg: {
    body: string;
    subject?: string | null;
    kind?: string;
    payload?: unknown;
  },
): Promise<MessageDelivery> {
  if (!msg.body || typeof msg.body !== 'string' || !msg.body.trim()) {
    return { ok: false, status: 400, error: 'body is required' };
  }

  const r = await query<SteerTaskRow>(
    `SELECT ${TASK_COLUMNS} FROM tasks WHERE id = $1`,
    [taskId],
  );
  const task = r.rows[0];
  if (!task) {
    return { ok: false, status: 404, error: 'task not found' };
  }
  const taskKey = task.task_key;

  // Operator messages are prompt requests. Sending one switches the task from
  // autonomous to interactive the first time (idempotent — the guard only
  // matches an autonomous task), signalling the agent to treat the message as
  // a new instruction and surface its answer in the Task Log.
  await query(
    `UPDATE tasks SET mode = 'interactive', updated_at = NOW()
      WHERE id = $1 AND mode = 'autonomous'`,
    [taskId],
  );

  const res = await fetch(`${WORKER_URL}/internal/tasks/operator/messages/send`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      to_task_key: taskKey,
      body: msg.body,
      subject: msg.subject || null,
      kind: msg.kind || 'request',
      payload: msg.payload || null,
    }),
  }).catch(() => null);

  if (!res) {
    return { ok: false, status: 502, error: 'worker unreachable' };
  }
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (!res.ok) {
    return {
      ok: false,
      status: res.status,
      error: String(data.detail || data.error || 'message send failed'),
    };
  }

  let triggered = false;
  if (CONTINUABLE_IDLE.has(task.status)) {
    try {
      const workerRes = await fetch(
        `${WORKER_URL}/internal/tasks/${encodeURIComponent(taskKey)}/continue`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: null, mode: null }),
        },
      );
      if (workerRes.ok) {
        const jobId = await enqueueTask({
          taskId,
          repoId: task.repo_id,
          taskKey,
          title: task.title,
          priority: task.priority,
          mode: 'interactive',
          claudeMode: task.claude_mode,
          maxTurns: task.max_turns ?? null,
          gitFlow: task.git_flow ?? 'branch',
        });
        await query(
          `UPDATE tasks SET status = 'queued', queue_job_id = $1, updated_at = NOW()
            WHERE id = $2`,
          [jobId, taskId],
        );
        triggered = true;
      }
    } catch (err) {
      // Best-effort: the message is already stored, so the operator can still
      // continue manually. Log the failure but don't lose the message.
      console.error('auto-continue after operator message failed', err);
    }
  }

  return { ok: true, triggered, data };
}
