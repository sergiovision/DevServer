/**
 * Task cancellation — shared by the dashboard (`POST /api/tasks/[id]/cancel`)
 * and the A2A gateway (`tasks/cancel`).
 *
 * Cancelling has two halves:
 *   1. drop the queued PgQueuer job, so a task that has not started never does;
 *   2. ask the worker to cancel the task, which also kills a running agent
 *      process — a status flip in the database alone would leave the CLI
 *      running and spending tokens.
 *
 * When the worker is down there is no agent to kill, so the status is set
 * directly in the database instead.
 */

import { query } from './db';
import { cancelTask } from './queue';
import { callWorker, readWorkerJson } from './worker-client';

export type CancelOutcome =
  | {
      ok: true;
      worker_reachable: boolean;
      agent_stopped: boolean;
      old_status: unknown;
      new_status: unknown;
    }
  | { ok: false; res: Response; body: Record<string, unknown> };

/** Remove the task's job from the queue if it has not been picked up yet. */
async function dropQueuedJob(queueJobId: string | null): Promise<void> {
  if (!queueJobId) return;
  try {
    await cancelTask(queueJobId);
  } catch {
    // The job may already be gone; the cancellation itself still proceeds.
  }
}

/** Fallback used only when the worker is unreachable. */
async function markCancelledInDatabase(taskId: number): Promise<void> {
  await query(
    `UPDATE tasks SET status = 'cancelled', updated_at = NOW() WHERE id = $1`,
    [taskId],
  );
}

/** Cancel a task end-to-end: queued job, running agent, task status. */
export async function cancelTaskFully(
  taskId: number,
  queueJobId: string | null,
): Promise<CancelOutcome> {
  await dropQueuedJob(queueJobId);

  const workerRes = await callWorker(`/internal/cancel/${taskId}`, { method: 'POST' });
  if (workerRes === null) {
    await markCancelledInDatabase(taskId);
    return {
      ok: true,
      worker_reachable: false,
      agent_stopped: false,
      old_status: null,
      new_status: 'cancelled',
    };
  }

  const body = await readWorkerJson(workerRes);
  if (!workerRes.ok) return { ok: false, res: workerRes, body };
  return {
    ok: true,
    worker_reachable: true,
    agent_stopped: body.agent_stopped === true,
    old_status: body.old_status ?? null,
    new_status: body.new_status ?? 'cancelled',
  };
}
