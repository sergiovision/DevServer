/**
 * Queue operations — enqueue/cancel/stats via PostgreSQL (PgQueuer).
 *
 * Replaces the old BullMQ + Valkey approach with direct SQL inserts
 * into the pgqueuer table.
 */

import { query } from './db';

export interface EnqueueTaskData {
  taskId: number;
  repoId: number;
  taskKey: string;
  title: string;
  priority: number;
  mode: string;
  claudeMode: string;
  maxTurns: number | null;
  gitFlow: string;
}

/**
 * Enqueue a task by inserting a row into the pgqueuer table.
 * Returns the pgqueuer job ID as a string.
 */
export async function enqueueTask(data: EnqueueTaskData): Promise<string> {
  const payload = JSON.stringify({
    taskId: data.taskId,
    repoId: data.repoId,
    taskKey: data.taskKey,
    title: data.title,
    mode: data.mode,
    claudeMode: data.claudeMode,
    maxTurns: data.maxTurns,
    gitFlow: data.gitFlow,
  });

  // PgQueuer priority: lower = higher priority. Our system: 1=critical, 4=low.
  // This maps directly — priority 1 (critical) dequeues before priority 4 (low).
  const result = await query<{ id: number }>(
    `INSERT INTO pgqueuer (entrypoint, payload, priority, status, created, updated, heartbeat, execute_after)
     VALUES ('devserver-tasks', $1, $2, 'queued', NOW(), NOW(), NOW(), NOW())
     RETURNING id`,
    [Buffer.from(payload), data.priority],
  );

  return String(result.rows[0].id);
}

/**
 * Cancel a queued job. Only removes jobs that haven't been picked up yet.
 */
export async function cancelTask(jobId: string): Promise<void> {
  // Only cancel if still queued (not yet picked by worker)
  await query(
    `UPDATE pgqueuer SET status = 'canceled', updated = NOW()
     WHERE id = $1 AND status = 'queued'`,
    [parseInt(jobId)],
  );
}

export interface QueueStats {
  waiting: number;
  active: number;
  completed: number;
  failed: number;
  delayed: number;
  paused: number;
}

/**
 * Get queue statistics from the pgqueuer table.
 */
export async function getQueueStats(): Promise<QueueStats> {
  const result = await query<{ status: string; count: string }>(
    `SELECT status::text, COUNT(*) as count FROM pgqueuer
     WHERE entrypoint = 'devserver-tasks'
     GROUP BY status`,
  );

  const counts: Record<string, number> = {};
  for (const row of result.rows) {
    counts[row.status] = parseInt(row.count);
  }

  // Check if paused via worker_state
  const pauseResult = await query<{ value: unknown }>(
    `SELECT value FROM worker_state WHERE key = 'paused'`,
  );
  const isPaused = pauseResult.rows.length > 0 && pauseResult.rows[0].value === true;

  return {
    waiting: counts['queued'] || 0,
    active: counts['picked'] || 0,
    completed: counts['successful'] || 0,
    failed: counts['exception'] || 0,
    delayed: 0, // PgQueuer doesn't have a separate delayed state
    paused: isPaused ? 1 : 0,
  };
}

/**
 * Pause the queue by setting a flag in worker_state.
 */
export async function pauseQueue(): Promise<void> {
  await query(
    `INSERT INTO worker_state (key, value, updated_at)
     VALUES ('paused', 'true', NOW())
     ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = NOW()`,
  );
}

/**
 * Resume the queue by clearing the paused flag.
 */
export async function resumeQueue(): Promise<void> {
  await query(
    `INSERT INTO worker_state (key, value, updated_at)
     VALUES ('paused', 'false', NOW())
     ON CONFLICT (key) DO UPDATE SET value = 'false', updated_at = NOW()`,
  );
}

