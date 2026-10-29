/**
 * Postgres implementation of `WebhookStore` (see `fire.ts`).
 *
 * Kept apart from the firing logic so that tests of `fireWebhook` never load
 * the database pool.
 */

import { query } from '../db';
import { enqueueStoredTask, insertTask, type NewTask } from '../tasks';
import type { TaskTemplate } from '../types';
import type { FiredTask, WebhookStore, WebhookTaskRequest, WebhookTrigger } from './fire';

async function findTriggerByToken(token: string): Promise<WebhookTrigger | null> {
  const result = await query<WebhookTrigger>(
    `SELECT id, name, repo_id, template_id, secret, title_template, description_template,
            task_key_prefix, priority, enabled
       FROM webhook_triggers
      WHERE token = $1`,
    [token],
  );
  return result.rows[0] ?? null;
}

async function loadTemplate(templateId: number | null): Promise<TaskTemplate | null> {
  if (templateId === null) return null;
  const result = await query<TaskTemplate>('SELECT * FROM task_templates WHERE id = $1', [templateId]);
  return result.rows[0] ?? null;
}

async function isLocalRepo(repoId: number): Promise<boolean> {
  const result = await query<{ provider: string }>('SELECT provider FROM repos WHERE id = $1', [repoId]);
  return result.rows[0]?.provider === 'local';
}

/**
 * The task row for a firing. Agent settings come from the trigger's template
 * when it has one; otherwise the same defaults as the task form. A local repo
 * cannot push, so its default output is a patch rather than a branch.
 */
async function buildNewTask(request: WebhookTaskRequest): Promise<NewTask> {
  const { trigger } = request;
  const template = await loadTemplate(trigger.template_id);
  const defaultGitFlow = (await isLocalRepo(trigger.repo_id)) ? 'patch' : 'branch';

  return {
    repoId: trigger.repo_id,
    taskKey: request.taskKey,
    title: request.title,
    description: request.description || template?.description || null,
    acceptance: template?.acceptance ?? null,
    priority: trigger.priority,
    labels: ['webhook'],
    mode: 'autonomous',
    taskType: template?.task_type ?? 'coding',
    claudeMode: template?.claude_mode ?? 'max',
    agentVendor: template?.agent_vendor ?? 'anthropic',
    claudeModel: template?.claude_model ?? null,
    maxTurns: template?.max_turns ?? null,
    skipVerify: template?.skip_verify ?? false,
    gitFlow: template?.git_flow ?? defaultGitFlow,
    backupVendor: template?.backup_vendor ?? null,
    backupModel: template?.backup_model ?? null,
    createdBy: 'webhook',
  };
}

async function createAndEnqueueTask(request: WebhookTaskRequest): Promise<FiredTask> {
  const task = await insertTask(await buildNewTask(request));
  await enqueueStoredTask(task);
  return { taskId: task.id, taskKey: task.task_key };
}

async function recordFiring(trigger: WebhookTrigger, task: FiredTask): Promise<void> {
  await query(
    `UPDATE webhook_triggers
        SET fire_count = fire_count + 1, last_fired_at = NOW(), updated_at = NOW()
      WHERE id = $1`,
    [trigger.id],
  );
  await query(
    `INSERT INTO task_events (task_id, event_type, payload) VALUES ($1, 'webhook_triggered', $2)`,
    [task.taskId, JSON.stringify({ trigger_id: trigger.id, trigger_name: trigger.name })],
  );
}

export const postgresWebhookStore: WebhookStore = {
  findTriggerByToken,
  createAndEnqueueTask,
  recordFiring,
};
