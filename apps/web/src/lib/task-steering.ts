/**
 * Task steering — how an operator redirects a task that is already in flight.
 *
 * `decidePlan` approves or rejects an interactive task's plan gate. Callers get
 * a discriminated union rather than an HTTP response, so the state transition
 * stays independent of how it is exposed (route handler today, other
 * surfaces later) and every surface maps failures the same way.
 *
 * It is also the home of "does this task need the operator?" — the rule the
 * sidebar's Needs You pill counts (`countTasksNeedingOperator`). The plan half
 * of that rule is `isAwaitingPlanDecision`, defined once next to the A2A state
 * mapping that first needed it and re-exported here, so the pill and an A2A
 * peer can never disagree about whether a plan is waiting.
 */

import { query } from './db';
import { isAwaitingPlanDecision, type TaskRow } from './a2a/mapping';

export { isAwaitingPlanDecision };

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

/** The columns the needs-operator rule reads — the SQL fetches only these. */
export type OperatorAttentionRow = Pick<
  TaskRow,
  'status' | 'mode' | 'has_plan' | 'plan_approved_at' | 'plan_rejected_at'
>;

export interface NeedsOperatorCounts {
  blocked: number;
  plans_awaiting_decision: number;
}

/**
 * Statuses in which an undecided plan is actually holding a live task. The
 * plan gate runs inside the heavy runner, so the task reads `running` while
 * it waits; a cancelled or failed task's stale plan must not nag forever.
 */
const PLAN_GATE_STATUSES = new Set(['running', 'verifying']);

/**
 * Count tasks waiting on a human. Each task lands in at most one bucket:
 * `blocked` wins, because a blocked task already shows up as such everywhere
 * and the plan gate itself sets `blocked` when a plan is rejected or times out.
 */
export function countTasksNeedingOperator(
  rows: readonly OperatorAttentionRow[],
): NeedsOperatorCounts {
  let blocked = 0;
  let plansAwaitingDecision = 0;
  for (const row of rows) {
    if (row.status === 'blocked') {
      blocked += 1;
    } else if (
      PLAN_GATE_STATUSES.has(row.status) &&
      // The predicate only reads the picked columns; the cast is needed until
      // mapping.ts narrows its parameter type to that Pick.
      isAwaitingPlanDecision(row as TaskRow)
    ) {
      plansAwaitingDecision += 1;
    }
  }
  return { blocked, plans_awaiting_decision: plansAwaitingDecision };
}
