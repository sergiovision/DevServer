/**
 * Free-text steering of an in-flight task by an A2A peer.
 *
 * A peer reply that is not a plan decision becomes an operator **note**
 * (`task_messages` kind `note`, see `apps/worker/src/services/task_notes.py`),
 * the same channel the dashboard's Notes panel uses. The runner injects unread
 * notes into the prompt at the start of each attempt.
 *
 * The worker only keeps a note deliverable while the task is active
 * (`queued`/`running`/`verifying`); a note posted to an idle task is archived
 * as already-read. So for a task that is idle but resumable (`blocked`, waiting
 * on guidance) the order matters:
 *
 *   1. worker `/continue` — resets the session for a follow-up attempt
 *   2. mark the task `queued` so the note counts as deliverable
 *   3. post the note
 *   4. enqueue the job
 *
 * Enqueueing last guarantees the runner cannot start the attempt before the
 * note is stored.
 */

import { query } from '@/lib/db';
import { enqueueTask } from '@/lib/queue';
import { callWorker, readWorkerJson } from '@/lib/worker-client';
import type { SteerFailure } from '@/lib/task-steering';

/**
 * Idle states that still hold a live session and may be resumed with a
 * follow-up attempt. A2A-terminal states (`test`, `failed`, `cancelled`, …)
 * never reach here — the gateway rejects steering those first.
 */
const RESUMABLE_IDLE = new Set(['blocked']);

/**
 * Why a `blocked` task must NOT be resumed by a peer, or null when it may be.
 *
 * `blocked` is DevServer's catch-all for "a human must decide". Some of those
 * decisions belong to the operator, not to the peer that created the task:
 *
 *   - an open side-effect gate (`decision_points` row, status `open`) — the
 *     peer resuming would bypass the operator's approve/reject;
 *   - a hard PR-preflight failure (secret in the diff, wrong author, oversized
 *     file) — the latest run's `pr_preflight_fail` carries a `hard` violation;
 *   - an exceeded budget ceiling (`budget_exceeded`) — the ceiling is the
 *     operator's governance, not the peer's to waive.
 *
 * The latest run is identified by the most recent `status_change → running`
 * event (`runner/phases.py`), so a block cause from an earlier, already
 * resolved run does not count.
 */
async function operatorOnlyBlockReason(taskId: number): Promise<string | null> {
  const open = await query<{ n: string }>(
    `SELECT COUNT(*)::text AS n FROM decision_points
      WHERE task_id = $1 AND status = 'open'`,
    [taskId],
  );
  if (parseInt(open.rows[0]?.n ?? '0', 10) > 0) {
    return 'task is waiting on an operator decision (side-effect gate) and cannot be resumed by a peer';
  }

  const last = await query<{ event_type: string; payload: Record<string, unknown> | null }>(
    `SELECT event_type, payload FROM task_events
      WHERE task_id = $1
        AND (event_type IN ('pr_preflight_fail', 'pr_preflight_pass', 'budget_exceeded')
             OR (event_type = 'status_change' AND payload->>'status' = 'running'))
      ORDER BY id DESC LIMIT 1`,
    [taskId],
  );
  const ev = last.rows[0];
  if (!ev) return null;
  if (ev.event_type === 'budget_exceeded') {
    return 'task exceeded its budget ceiling; only an operator can resume it';
  }
  if (ev.event_type === 'pr_preflight_fail') {
    const violations = Array.isArray(ev.payload?.violations) ? ev.payload.violations : [];
    const hard = violations.some(
      (v) => !!v && typeof v === 'object' && (v as { severity?: unknown }).severity === 'hard',
    );
    if (hard) {
      return 'task was blocked by a hard PR preflight failure; only an operator can resume it';
    }
  }
  return null;
}

export type NoteDelivery = { ok: true; resumed: boolean } | SteerFailure;

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

async function postNote(taskKey: string, body: string): Promise<SteerFailure | null> {
  const res = await callWorker(`/internal/tasks/${encodeURIComponent(taskKey)}/notes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ body }),
  });
  if (res === null) return { ok: false, status: 502, error: 'worker unreachable' };
  if (!res.ok) {
    const data = await readWorkerJson(res);
    return {
      ok: false,
      status: res.status,
      error: String(data.detail ?? data.error ?? 'note delivery failed'),
    };
  }
  return null;
}

/** Deliver `body` to the task's agent, resuming the task when it is idle. */
export async function deliverPeerNote(taskId: number, body: string): Promise<NoteDelivery> {
  const text = body.trim();
  if (!text) return { ok: false, status: 400, error: 'body is required' };

  const r = await query<SteerTaskRow>(
    `SELECT id, task_key, status, repo_id, title, priority, mode, claude_mode,
            max_turns, git_flow
       FROM tasks WHERE id = $1`,
    [taskId],
  );
  const task = r.rows[0];
  if (!task) return { ok: false, status: 404, error: 'task not found' };

  if (!RESUMABLE_IDLE.has(task.status)) {
    // Active (or still pending): store the note; an active runner picks it up
    // on its next attempt.
    const failed = await postNote(task.task_key, text);
    if (failed) return failed;
    return { ok: true, resumed: false };
  }

  // Operator-owned blocks are refused outright (403-ish), before any state
  // changes — see operatorOnlyBlockReason.
  const refused = await operatorOnlyBlockReason(task.id);
  if (refused) return { ok: false, status: 409, error: refused };

  const cont = await callWorker(
    `/internal/tasks/${encodeURIComponent(task.task_key)}/continue`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: null, mode: null }),
    },
  );
  if (cont === null) return { ok: false, status: 502, error: 'worker unreachable' };
  if (!cont.ok) {
    const data = await readWorkerJson(cont);
    return {
      ok: false,
      status: cont.status,
      error: String(data.detail ?? data.error ?? 'continue failed'),
    };
  }

  await query(`UPDATE tasks SET status = 'queued', updated_at = NOW() WHERE id = $1`, [
    task.id,
  ]);

  // Enqueue even if the note fails: /continue has already reset the task, and
  // leaving it `queued` with no job would strand it.
  const noteFailed = await postNote(task.task_key, text);

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
  await query(`UPDATE tasks SET queue_job_id = $1, updated_at = NOW() WHERE id = $2`, [
    jobId,
    task.id,
  ]);
  if (noteFailed) return noteFailed;
  return { ok: true, resumed: true };
}
