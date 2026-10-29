/**
 * Task persistence shared by every place that creates or enqueues a task:
 * the task form (`POST /api/tasks`), the Enqueue button, and webhook triggers.
 *
 * Keeping the INSERT and the "enqueue + mark queued" step in one place means
 * a new task column or a change to the queue payload is made once, not once
 * per entry point.
 */

import { query } from './db';
import { enqueueTask } from './queue';
import type { Task } from './types';

/** Everything needed to insert a task row. Optional fields use DB defaults. */
export interface NewTask {
  repoId: number | null;
  taskKey: string;
  title: string;
  description: string | null;
  acceptance: string | null;
  priority: number;
  labels: string[];
  mode: string;
  taskType: string;
  claudeMode: string;
  agentVendor: string;
  claudeModel: string | null;
  maxTurns: number | null;
  skipVerify: boolean;
  gitFlow: string;
  backupVendor: string | null;
  backupModel: string | null;
  /** Who created the task — shown in the UI and useful for auditing. */
  createdBy?: string;
}

/** Insert a task in `pending` status and return the stored row. */
export async function insertTask(task: NewTask): Promise<Task> {
  const result = await query<Task>(
    `INSERT INTO tasks (repo_id, task_key, title, description, acceptance, priority, labels,
                        mode, task_type, claude_mode, agent_vendor, claude_model, max_turns,
                        skip_verify, git_flow, backup_vendor, backup_model, created_by, status)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, 'pending')
     RETURNING *`,
    [
      task.repoId,
      task.taskKey,
      task.title,
      task.description,
      task.acceptance,
      task.priority,
      task.labels,
      task.mode,
      task.taskType,
      task.claudeMode,
      task.agentVendor,
      task.claudeModel,
      task.maxTurns,
      task.skipVerify,
      task.gitFlow,
      task.backupVendor,
      task.backupModel,
      task.createdBy ?? 'ui',
    ],
  );
  return result.rows[0];
}

/** The task fields the queue payload is built from. */
export type EnqueueableTask = Pick<
  Task,
  'id' | 'repo_id' | 'task_key' | 'title' | 'priority' | 'mode' | 'claude_mode' | 'max_turns' | 'git_flow'
>;

/**
 * Put a stored task on the queue and mark it `queued`.
 * Returns the queue job id.
 */
export async function enqueueStoredTask(task: EnqueueableTask): Promise<string> {
  const jobId = await enqueueTask({
    taskId: task.id,
    repoId: task.repo_id,
    taskKey: task.task_key,
    title: task.title,
    priority: task.priority,
    mode: task.mode,
    claudeMode: task.claude_mode,
    maxTurns: task.max_turns ?? null,
    gitFlow: task.git_flow ?? 'branch',
  });
  await query(
    `UPDATE tasks SET status = 'queued', queue_job_id = $1, updated_at = NOW() WHERE id = $2`,
    [jobId, task.id],
  );
  return jobId;
}

/** The task key for a task id, or null when the task does not exist. */
export async function findTaskKey(taskId: number): Promise<string | null> {
  const result = await query<{ task_key: string }>(
    'SELECT task_key FROM tasks WHERE id = $1',
    [taskId],
  );
  return result.rows[0]?.task_key ?? null;
}
