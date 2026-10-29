/**
 * Task steering — how an operator redirects a task that is already in flight.
 *
 * `decidePlan` approves or rejects an interactive task's plan gate. Callers get
 * a discriminated union rather than an HTTP response, so the state transition
 * stays independent of how it is exposed (route handler today, other
 * surfaces later) and every surface maps failures the same way.
 */

import { query } from './db';

export type SteerFailure = { ok: false; status: number; error: string };
export type PlanDecision =
  | { ok: true; action: 'approve' | 'reject' }
  | SteerFailure;

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
