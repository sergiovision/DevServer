/**
 * DevServer task ⇄ A2A Task translation.
 *
 * This is the heart of the adapter: DevServer's lifecycle and A2A's TaskState
 * are close but not identical, and the mismatches all live here.
 */

import { query } from '@/lib/db';
import { sanitizeForPeer, sanitizeJsonForPeer } from './redact';
import {
  A2AArtifact,
  A2APart,
  A2ATask,
  A2ATaskState,
  dataPart,
  filePart,
  textPart,
} from './protocol';

export interface TaskRow {
  id: number;
  task_key: string;
  repo_id: number | null;
  title: string;
  description: string | null;
  acceptance: string | null;
  status: string;
  mode: string;
  task_type: string;
  git_flow: string | null;
  priority: number;
  created_by: string | null;
  plan_approved_at: string | null;
  plan_rejected_at: string | null;
  abstain_reason: string | null;
  reality_score: number | null;
  suspended_reason: string | null;
  created_at: string;
  updated_at: string;
  repo_name?: string | null;
  /** True when some run of this task has produced a plan. See `isAwaitingPlanDecision`. */
  has_plan: boolean;
}

export interface RunRow {
  attempt: number;
  status: string;
  branch: string | null;
  pr_url: string | null;
  cost_usd: string | null;
  duration_ms: string | null;
  turns: number | null;
  error_log: string | null;
  claude_output: string | null;
  plan_json: unknown;
  started_at: string | null;
  finished_at: string | null;
}

/**
 * Map a DevServer status to an A2A TaskState.
 *
 * Two DevServer-specific subtleties:
 *
 *  - The worker never writes ``done``. On success it writes **``test``**
 *    (``services/runner/outcome.py`` for the heavy runner,
 *    ``services/skill_runner.py`` for skill/research), meaning "succeeded,
 *    awaiting human QA, branch
 *    and session still live". ``done``/``retired`` are set by a human in the
 *    dashboard. All three are ``completed`` to a peer — the work is finished.
 *  - ``blocked`` is DevServer's catch-all for "a human must decide" (plan
 *    rejected, budget exceeded, preflight failure). That is exactly A2A's
 *    ``input-required``, which lets the calling agent supply the decision.
 *
 * KEEP IN SYNC with ``_a2a_state`` in
 * ``apps/worker/src/services/notify/a2a_backend.py`` (push notifications) —
 * this function is the authority.
 */
export function toA2AState(task: TaskRow): A2ATaskState {
  // The reality gate refused to spend an agent run at all — that is a refusal
  // to accept the work, not a failure of it.
  if (task.abstain_reason) return 'rejected';

  switch (task.status) {
    case 'pending':
    case 'queued':
      return 'submitted';
    case 'running':
    case 'verifying':
      // An interactive task sitting on an undecided plan is waiting on us, even
      // though the worker coroutine is technically still "running".
      return isAwaitingPlanDecision(task) ? 'input-required' : 'working';
    case 'test':
    case 'done':
    case 'retired':
      return 'completed';
    case 'failed':
      return 'failed';
    case 'cancelled':
      return 'canceled';
    case 'blocked':
      return 'input-required';
    default:
      return 'unknown';
  }
}

/**
 * True when the plan gate is actually holding this task for an approve/reject.
 *
 * `mode === 'interactive'` alone is NOT sufficient. Only the heavy runner
 * (`agent_runner.run_task`) runs a plan gate; `skill_runner` — which handles
 * every `skill` and `research` task — has none. So a research task created by
 * a peer whose credential forces plan approval is interactive but never
 * produces a plan, and reporting it `input-required` while it runs happily
 * would strand the caller waiting to approve something that does not exist.
 *
 * Requiring a persisted plan (`task_runs.plan_json`) makes the predicate mean
 * what it says.
 */
export function isAwaitingPlanDecision(task: TaskRow): boolean {
  return (
    task.mode === 'interactive' &&
    task.has_plan &&
    !task.plan_approved_at &&
    !task.plan_rejected_at
  );
}

/**
 * Human-readable explanation of why a task needs input. Surfaced as the status
 * message so an orchestrator can decide without a second round-trip.
 */
function inputRequiredReason(
  task: TaskRow,
  run: RunRow | null,
  peerMayApprove: boolean,
): string {
  if (isAwaitingPlanDecision(task) && run?.plan_json) {
    return peerMayApprove
      ? 'A plan has been produced and needs approval. Reply with "approve" or "reject".'
      : 'A plan has been produced and is awaiting approval by the DevServer operator. ' +
          'You may reply "reject" to cancel it; approval is reserved for the operator.';
  }
  if (task.suspended_reason) return sanitizeForPeer(task.suspended_reason);
  if (run?.error_log) {
    return `Task is blocked and needs guidance. Last error: ${sanitizeForPeer(
      run.error_log,
    ).slice(0, 1000)}`;
  }
  return 'Task is blocked and needs operator guidance.';
}

/**
 * Build the artifact list from a finished (or in-progress) run.
 *
 * Every text Part is passed through the egress filter — agent stdout is raw
 * output from a repo full of credentials.
 */
export function buildArtifacts(
  task: TaskRow,
  run: RunRow | null,
  origin: string,
): A2AArtifact[] {
  const artifacts: A2AArtifact[] = [];
  if (!run) return artifacts;

  if (run.pr_url) {
    artifacts.push({
      artifactId: `${task.task_key}-pr`,
      name: 'pull-request',
      description: 'Pull request opened for this task',
      parts: [filePart(run.pr_url, 'pull-request', 'text/html')],
    });
  }

  // Patch-mode tasks export a `git format-patch` series instead of pushing.
  // The URL lists the exported files (`{ patches: [{ name, size_bytes }] }`);
  // each one downloads from `<url>/<name>`. It is the peer-scoped route under
  // /api/a2a (bearer auth + created_by tenancy), never the dashboard's
  // /api/tasks/<id>/patches, which is an operator surface.
  if (task.git_flow === 'patch') {
    artifacts.push({
      artifactId: `${task.task_key}-patches`,
      name: 'patches',
      description:
        'git format-patch series for this task. GET the URL for the file list, ' +
        'then GET <url>/<name> for each file and apply with `git am`.',
      parts: [
        filePart(
          `${origin}/api/a2a/tasks/${task.id}/patches`,
          'patches',
          'application/json',
        ),
      ],
      ...(run.branch && { metadata: { branch: run.branch } }),
    });
  }

  // The agent's final answer. For skill/research tasks this holds RESULT.md and
  // IS the deliverable, so it is never omitted.
  if (run.claude_output) {
    artifacts.push({
      artifactId: `${task.task_key}-output`,
      name: 'agent-output',
      description: 'Final agent output',
      parts: [textPart(sanitizeForPeer(run.claude_output).slice(0, 100_000))],
    });
  }

  if (run.error_log && run.status === 'failed') {
    artifacts.push({
      artifactId: `${task.task_key}-error`,
      name: 'error-log',
      description: 'Error output from the failed run',
      parts: [textPart(sanitizeForPeer(run.error_log).slice(0, 20_000))],
    });
  }

  return artifacts;
}

/** Fetch the most recent run for a task, or null when it has never run. */
export async function fetchLatestRun(taskId: number): Promise<RunRow | null> {
  const res = await query<RunRow>(
    `SELECT attempt, status, branch, pr_url, cost_usd, duration_ms, turns,
            error_log, claude_output, plan_json, started_at, finished_at
       FROM task_runs WHERE task_id = $1
      ORDER BY attempt DESC LIMIT 1`,
    [taskId],
  );
  return res.rows[0] ?? null;
}

/**
 * Assemble the full A2A Task object.
 *
 * ``contextId`` is the repo id so that a peer working a single repo sees all
 * its tasks share a context — the closest natural analogue DevServer has to
 * A2A's conversation grouping.
 */
export function toA2ATask(
  task: TaskRow,
  run: RunRow | null,
  origin: string,
  /** False for a peer whose credential requires operator plan approval. */
  peerMayApprove = true,
): A2ATask {
  const state = toA2AState(task);
  const parts: A2APart[] = [];

  if (state === 'input-required') {
    parts.push(textPart(inputRequiredReason(task, run, peerMayApprove)));
    if (isAwaitingPlanDecision(task) && run?.plan_json) {
      // The plan is agent-written text (summary, steps, file paths) and leaves
      // the deployment, so it goes through the same egress filter as stdout.
      parts.push(dataPart(sanitizeJsonForPeer(run.plan_json)));
    }
  } else if (state === 'rejected' && task.abstain_reason) {
    parts.push(textPart(sanitizeForPeer(task.abstain_reason)));
  } else if (state === 'failed' && run?.error_log) {
    parts.push(textPart(sanitizeForPeer(run.error_log).slice(0, 4000)));
  }

  return {
    kind: 'task',
    id: String(task.id),
    contextId: task.repo_id !== null ? `repo-${task.repo_id}` : 'no-repo',
    status: {
      state,
      timestamp: new Date(task.updated_at).toISOString(),
      ...(parts.length > 0 && {
        message: {
          kind: 'message' as const,
          messageId: `${task.task_key}-status-${Date.parse(task.updated_at)}`,
          role: 'agent' as const,
          taskId: String(task.id),
          parts,
        },
      }),
    },
    artifacts: buildArtifacts(task, run, origin),
    metadata: {
      taskKey: task.task_key,
      taskType: task.task_type,
      devserverStatus: task.status,
      repoName: task.repo_name ?? null,
      attempt: run?.attempt ?? 0,
      costUsd: run?.cost_usd ? Number(run.cost_usd) : null,
      turns: run?.turns ?? null,
      realityScore: task.reality_score,
    },
  };
}

export const TASK_SELECT_COLUMNS = `t.id, t.task_key, t.repo_id, t.title, t.description,
        t.acceptance, t.status, t.mode, t.task_type, t.git_flow, t.priority, t.created_by,
        t.plan_approved_at, t.plan_rejected_at, t.abstain_reason, t.reality_score,
        t.suspended_reason, t.created_at, t.updated_at, r.name AS repo_name,
        EXISTS (SELECT 1 FROM task_runs tr
                 WHERE tr.task_id = t.id AND tr.plan_json IS NOT NULL) AS has_plan`;
