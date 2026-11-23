import { NextRequest, NextResponse } from 'next/server';
import crypto from 'crypto';
import { query } from '@/lib/db';
import { cancelTask, enqueueTask } from '@/lib/queue';
import { defaultModelFor, fetchAgentRegistry } from '@/lib/agent-vendors';
import {
  A2APeer,
  authenticatePeer,
  consumeTaskQuota,
  peerCreatedBy,
  peerMayUseRepo,
  peerMayUseTaskType,
} from '@/lib/a2a/auth';
import { buildExtendedCard } from '@/lib/a2a/card';
import {
  TASK_SELECT_COLUMNS,
  TaskRow,
  fetchLatestRun,
  isAwaitingPlanDecision,
  toA2AState,
  toA2ATask,
} from '@/lib/a2a/mapping';
import { streamCapReached, streamTask } from '@/lib/a2a/stream';
import {
  createPushConfig,
  deletePushConfig,
  getPushConfig,
  listPushConfigs,
} from '@/lib/a2a/push';
import { publicOrigin } from '@/lib/a2a/origin';
import { decidePlan } from '@/lib/task-steering';
import { cancelTaskFully } from '@/lib/task-cancel';
import { deliverPeerNote } from '@/lib/a2a/steer';
import { sanitiseTaskFields } from '@/lib/injection-ingress';
import { recordInjectionEvent } from '@/lib/injection-ingress-db';
import {
  A2A_TERMINAL_STATES,
  JsonRpcRequest,
  RPC,
  RpcError,
  partsToText,
  rpcError,
  rpcResult,
} from '@/lib/a2a/protocol';

/**
 * A2A 1.0 JSON-RPC 2.0 endpoint.
 *
 *   POST /api/a2a     Authorization: Bearer <peer token>
 *
 * This is the only externally-reachable execution surface in DevServer. It is
 * deliberately in the Next.js tier rather than the worker: the worker has no
 * authentication on any route and serves `/internal/env` (cleartext secrets)
 * from the same flat namespace, so it must never be exposed. Next.js is also
 * where `enqueueTask` lives — the sole legal producer for the pgqueuer table.
 *
 * Methods: message/send (create or steer), message/stream, tasks/get,
 * tasks/list, tasks/cancel, tasks/subscribe, tasks/resubscribe,
 * tasks/pushNotificationConfig/{create,set,get,list,delete},
 * agent/getExtendedAgentCard.
 */

const TASK_TYPES = ['coding', 'test', 'skill', 'script', 'research'];
const REPO_REQUIRED_TYPES = new Set(['coding', 'test', 'script']);

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

// ─── Task creation ───────────────────────────────────────────────────────────

/**
 * Resolve the target repo from the peer's request.
 *
 * A2A has no notion of a repository, so we accept it three ways, in descending
 * order of explicitness, and fall back to the peer's sole allowed repo when it
 * has exactly one (the common single-project case).
 */
/**
 * One error for "no such repo" and "not yours" — a peer must not be able to
 * enumerate repository ids or names by probing for a different message.
 */
function repoNotAuthorized(): RpcError {
  return new RpcError(
    RPC.UNSUPPORTED_OPERATION,
    'this peer is not authorized for the requested repository',
  );
}

async function resolveRepoId(
  peer: A2APeer,
  meta: Record<string, unknown>,
  contextId: unknown,
): Promise<number | null> {
  if (meta.repoId !== undefined && meta.repoId !== null) {
    const id =
      typeof meta.repoId === 'string' && /^\d+$/.test(meta.repoId.trim())
        ? parseInt(meta.repoId.trim(), 10)
        : meta.repoId;
    if (typeof id !== 'number' || !Number.isSafeInteger(id) || id < 1) {
      throw new RpcError(RPC.INVALID_PARAMS, 'metadata.repoId must be a positive integer');
    }
    return id;
  }

  if (typeof meta.repoName === 'string' && meta.repoName.trim()) {
    // Looked up only within the peer's own scope, so an existing-but-foreign
    // repo and a non-existent one are indistinguishable.
    const res = await query<{ id: number }>(
      `SELECT id FROM repos WHERE name = $1 AND id = ANY($2::int[])`,
      [meta.repoName.trim(), peer.allowed_repo_ids || []],
    );
    if (res.rows.length === 0) throw repoNotAuthorized();
    return res.rows[0].id;
  }

  // contextId round-trips from a previous response (`repo-<id>`).
  if (typeof contextId === 'string') {
    const m = /^repo-(\d+)$/.exec(contextId);
    if (m) return parseInt(m[1], 10);
  }

  const allowed = peer.allowed_repo_ids || [];
  return allowed.length === 1 ? allowed[0] : null;
}

function resolveTaskType(peer: A2APeer, meta: Record<string, unknown>): string {
  if (typeof meta.taskType === 'string') {
    if (!TASK_TYPES.includes(meta.taskType)) {
      throw new RpcError(
        RPC.INVALID_PARAMS,
        `taskType must be one of: ${TASK_TYPES.join(', ')}`,
      );
    }
    return meta.taskType;
  }
  const allowed = peer.allowed_task_types || [];
  if (allowed.length === 1) return allowed[0];
  if (allowed.includes('research')) return 'research';
  throw new RpcError(
    RPC.INVALID_PARAMS,
    `metadata.taskType is required; this peer may use: ${allowed.join(', ')}`,
  );
}

/** `{prefix}-{YYMMDDHHMMSS}-{hex4}` — mirrors the webhook trigger key format. */
function generateTaskKey(prefix: string): string {
  const now = new Date();
  const stamp =
    now.getUTCFullYear().toString().slice(2) +
    String(now.getUTCMonth() + 1).padStart(2, '0') +
    String(now.getUTCDate()).padStart(2, '0') +
    String(now.getUTCHours()).padStart(2, '0') +
    String(now.getUTCMinutes()).padStart(2, '0') +
    String(now.getUTCSeconds()).padStart(2, '0');
  return `${prefix}-${stamp}-${crypto.randomBytes(2).toString('hex')}`;
}

/** Take the tighter of the peer ceiling and any client-requested limit. */
function clampCeiling(
  peerLimit: string | number | null,
  requested: unknown,
): number | null {
  const peerVal = peerLimit === null ? null : Number(peerLimit);
  const reqVal = typeof requested === 'number' && requested > 0 ? requested : null;
  if (peerVal === null) return reqVal;
  if (reqVal === null) return peerVal;
  return Math.min(peerVal, reqVal);
}

async function handleMessageSend(
  peer: A2APeer,
  params: unknown,
  origin: string,
) {
  const p = asRecord(params);
  const message = asRecord(p.message);
  if (!message.parts) {
    throw new RpcError(RPC.INVALID_PARAMS, 'message.parts is required');
  }

  // A message naming an existing task is a reply, not a new request — that is
  // the input-required loop, handled separately.
  if (message.taskId) {
    return handleSteer(peer, message.taskId, message, origin);
  }

  const text = partsToText(message.parts);
  if (!text) {
    throw new RpcError(
      RPC.INVALID_PARAMS,
      'message must contain at least one non-empty text part',
    );
  }

  const meta = asRecord(message.metadata ?? p.metadata);
  const taskType = resolveTaskType(peer, meta);
  if (!peerMayUseTaskType(peer, taskType)) {
    throw new RpcError(
      RPC.UNSUPPORTED_OPERATION,
      `this peer is not authorized for task type '${taskType}'`,
    );
  }

  const repoId = await resolveRepoId(peer, meta, message.contextId ?? p.contextId);
  if (REPO_REQUIRED_TYPES.has(taskType) && repoId === null) {
    throw new RpcError(
      RPC.INVALID_PARAMS,
      `a repository is required for '${taskType}' tasks — set metadata.repoId or metadata.repoName`,
    );
  }
  if (repoId !== null && !peerMayUseRepo(peer, repoId)) {
    // Same error whether the repo exists or not — do not let a peer enumerate
    // repository ids by probing for a different error message.
    throw repoNotAuthorized();
  }

  const title =
    typeof meta.title === 'string' && meta.title.trim()
      ? meta.title.trim().slice(0, 500)
      : text.split('\n')[0].slice(0, 200);
  const acceptance =
    typeof meta.acceptance === 'string' ? meta.acceptance.slice(0, 10_000) : null;
  // A peer is an external system: clean its text before it is stored
  // (invisible characters removed, override attempts flagged, never refused).
  const scan = sanitiseTaskFields('a2a', {
    title,
    description: text.slice(0, 100_000),
    acceptance,
  });

  // Governance: the peer's ceilings are stamped onto the task, never negotiated
  // away by the caller. mode='interactive' routes through the plan gate so a
  // human sees the plan before any code runs; git_flow='patch' never pushes.
  const mode = peer.require_plan_approval ? 'interactive' : 'autonomous';
  const maxCost = clampCeiling(peer.max_cost_usd, meta.maxCostUsd);
  const maxWall = clampCeiling(peer.max_wall_seconds, meta.maxWallSeconds);
  const taskKey = generateTaskKey(peer.task_key_prefix);
  // A peer need not pin a model, but the task row must still carry a concrete
  // one: the CLI rejects an empty model string ("400 model: String should have
  // at least 1 character") rather than falling back to a vendor default.
  const model =
    peer.default_model ||
    defaultModelFor(
      await fetchAgentRegistry().catch(() => null),
      peer.default_agent_vendor,
    ) ||
    null;

  // Quota is consumed last, after every validation and lookup, and refunded
  // if creating/enqueueing the task fails — so an internal error neither
  // costs the peer a task nor leaves an orphaned never-enqueued row behind.
  if (!(await consumeTaskQuota(peer.id))) {
    throw new RpcError(
      RPC.UNSUPPORTED_OPERATION,
      `daily task quota exhausted (${peer.daily_task_quota}/day)`,
    );
  }

  let taskId: number | null = null;
  let jobId: string | null = null;
  try {
    const inserted = await query<{ id: number }>(
      `INSERT INTO tasks
         (repo_id, task_key, title, description, acceptance, priority, mode,
          task_type, agent_vendor, claude_model, git_flow, max_cost_usd,
          max_wall_seconds, status, created_by)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'pending', $14)
       RETURNING id`,
      [
        repoId,
        taskKey,
        scan.fields.title,
        scan.fields.description,
        scan.fields.acceptance,
        peer.priority,
        mode,
        taskType,
        peer.default_agent_vendor,
        model,
        peer.force_git_flow,
        maxCost,
        maxWall,
        peerCreatedBy(peer),
      ],
    );
    taskId = inserted.rows[0].id;

    // Audit trail. Rides the existing task_events → PG NOTIFY → WebSocket path,
    // so the task appears in the dashboard timeline with no extra plumbing.
    await query(
      `INSERT INTO task_events (task_id, event_type, payload)
       VALUES ($1, 'a2a_task_created', CAST($2 AS JSONB))`,
      [
        taskId,
        JSON.stringify({
          peer: peer.slug,
          peer_name: peer.name,
          task_type: taskType,
          repo_id: repoId,
          mode,
          git_flow: peer.force_git_flow,
          max_cost_usd: maxCost,
        }),
      ],
    ).catch(() => {}); // audit failure must not fail the task
    await recordInjectionEvent(taskId, scan.event);

    jobId = await enqueueTask({
      taskId,
      repoId,
      taskKey,
      title: scan.fields.title,
      priority: peer.priority,
      mode,
      // Subscription billing: the CLI reports cost_usd=0, so max_cost_usd is
      // recorded but NOT enforced for A2A tasks — only max_wall_seconds is.
      // The agent card and peers UI say so (P-M2).
      claudeMode: 'max',
      maxTurns: null,
      gitFlow: peer.force_git_flow,
    });
    await query(
      `UPDATE tasks SET status = 'queued', queue_job_id = $1, updated_at = NOW() WHERE id = $2`,
      [jobId, taskId],
    );
  } catch (err) {
    // Undo: refund the quota and close out whatever was half-created so the
    // peer is not charged and no orphaned pending task (or job) remains.
    await refundTaskQuota(peer.id).catch(() => {});
    if (jobId !== null) await cancelTask(jobId).catch(() => {});
    if (taskId !== null) {
      await query(
        `UPDATE tasks SET status = 'failed', updated_at = NOW() WHERE id = $1`,
        [taskId],
      ).catch(() => {});
    }
    throw err;
  }

  return loadTask(peer, String(taskId), origin);
}

/** Give back one unit of quota consumed by a creation that then failed. */
async function refundTaskQuota(peerId: number): Promise<void> {
  await query(
    `UPDATE a2a_peers
        SET tasks_today = GREATEST(tasks_today - 1, 0),
            task_count = GREATEST(task_count - 1, 0),
            updated_at = NOW()
      WHERE id = $1 AND quota_reset_on = CURRENT_DATE`,
    [peerId],
  );
}

// ─── Steering an in-flight task (the input-required loop) ────────────────────

/**
 * Interpret a peer's reply to a plan gate.
 *
 * `metadata.action` is the unambiguous form; the text forms exist because an
 * LLM-driven orchestrator naturally replies in prose and should not have to
 * know a DevServer-specific metadata key to say yes.
 */
function resolveDecision(action: unknown, text: string): 'approve' | 'reject' | null {
  if (action === 'approve' || action === 'reject') return action;
  const t = text.trim().toLowerCase();
  if (/^(approve|approved|yes|lgtm|ok|okay|proceed|go ahead)\b/.test(t)) return 'approve';
  if (/^(reject|rejected|no|deny|denied|abort|stop)\b/.test(t)) return 'reject';
  return null;
}

/**
 * Handle `message/send` against an existing task.
 *
 * Two shapes, both reusing the dashboard's own mechanisms (`decidePlan` and
 * operator notes) so an external agent drives the exact same state
 * transitions as an operator clicking in the dashboard:
 *
 *   - the task is waiting on a plan → approve/reject the plan gate
 *   - anything else non-terminal    → leave an operator note and, when the
 *                                     task is blocked, resume it
 */
async function handleSteer(
  peer: A2APeer,
  taskIdRaw: unknown,
  message: Record<string, unknown>,
  origin: string,
) {
  const task = await loadTaskRow(peer, String(taskIdRaw));
  const state = toA2AState(task);
  if (A2A_TERMINAL_STATES.has(state)) {
    throw new RpcError(
      RPC.UNSUPPORTED_OPERATION,
      `task is in a terminal state (${state}) and cannot be steered; send a new message to create a follow-up task`,
    );
  }

  const text = partsToText(message.parts);
  const meta = asRecord(message.metadata);

  if (isAwaitingPlanDecision(task)) {
    const action = resolveDecision(meta.action, text);
    if (!action) {
      throw new RpcError(
        RPC.INVALID_PARAMS,
        peer.require_plan_approval
          ? 'this task is waiting on the operator to approve its plan — you may only ' +
              'reply "reject" (or set metadata.action to "reject")'
          : 'this task is waiting on a plan decision — reply "approve" or "reject" ' +
              '(or set metadata.action to one of them)',
      );
    }
    // A peer whose credential requires plan approval must not approve its own
    // plan — that gate exists precisely so a *human* sees the plan first. It
    // may still reject (abort) it; the task stays input-required until the
    // operator decides in the dashboard.
    if (action === 'approve' && peer.require_plan_approval) {
      throw new RpcError(
        RPC.UNSUPPORTED_OPERATION,
        'plan approval for this peer is reserved for the DevServer operator; ' +
          'you may reply "reject" to cancel the plan',
      );
    }
    const decided = await decidePlan(task.id, action);
    if (!decided.ok) {
      throw new RpcError(
        decided.status === 409 ? RPC.UNSUPPORTED_OPERATION : RPC.INVALID_PARAMS,
        decided.error,
      );
    }
    await auditSteer(task.id, peer, { action });
    return loadTask(peer, String(task.id), origin);
  }

  // The gate is not open. An *explicit* metadata.action must not be silently
  // downgraded into a chat message: the peer asked for a decision, and quietly
  // doing something else would have it believe it approved or rejected a plan
  // that it did not.
  if (meta.action === 'approve' || meta.action === 'reject') {
    const decided =
      task.plan_approved_at ? 'approved' : task.plan_rejected_at ? 'rejected' : null;
    throw new RpcError(
      RPC.UNSUPPORTED_OPERATION,
      decided
        ? `plan was already ${decided}; send a message without metadata.action to steer this task`
        : 'this task is not waiting on a plan decision',
    );
  }

  if (!text) {
    throw new RpcError(
      RPC.INVALID_PARAMS,
      'message must contain at least one non-empty text part',
    );
  }

  const delivered = await deliverPeerNote(task.id, text);
  if (!delivered.ok) {
    throw new RpcError(
      delivered.status >= 500
        ? RPC.INTERNAL_ERROR
        : delivered.status === 409
          ? RPC.UNSUPPORTED_OPERATION
          : RPC.INVALID_PARAMS,
      delivered.error,
    );
  }
  await auditSteer(task.id, peer, { resumed: delivered.resumed });
  return loadTask(peer, String(task.id), origin);
}

async function auditSteer(taskId: number, peer: A2APeer, detail: Record<string, unknown>) {
  await query(
    `INSERT INTO task_events (task_id, event_type, payload)
     VALUES ($1, 'a2a_task_steered', CAST($2 AS JSONB))`,
    [taskId, JSON.stringify({ peer: peer.slug, ...detail })],
  ).catch(() => {});
}

// ─── Push notification configs ───────────────────────────────────────────────

/** Resolve + authorize the task id every pushNotificationConfig call carries. */
async function pushTaskId(peer: A2APeer, params: unknown): Promise<number> {
  const p = asRecord(params);
  const raw =
    p.taskId ?? p.id ?? asRecord(p.pushNotificationConfig).taskId;
  if (typeof raw !== 'string' && typeof raw !== 'number') {
    throw new RpcError(RPC.INVALID_PARAMS, 'params.taskId is required');
  }
  const task = await loadTaskRow(peer, String(raw));
  return task.id;
}

// ─── Task reads ──────────────────────────────────────────────────────────────

/**
 * Fetch one task, scoped to the calling peer.
 *
 * The `created_by` filter is the tenancy boundary: a peer can only ever see
 * tasks it created, so a guessed id leaks nothing.
 */
async function loadTaskRow(peer: A2APeer, id: string): Promise<TaskRow> {
  const numericId = parseInt(id, 10);
  if (!Number.isInteger(numericId)) {
    throw new RpcError(RPC.TASK_NOT_FOUND, `task not found: ${id}`);
  }
  const res = await query<TaskRow>(
    `SELECT ${TASK_SELECT_COLUMNS}
       FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id
      WHERE t.id = $1 AND t.created_by = $2`,
    [numericId, peerCreatedBy(peer)],
  );
  if (res.rows.length === 0) {
    throw new RpcError(RPC.TASK_NOT_FOUND, `task not found: ${id}`);
  }
  return res.rows[0];
}

async function loadTask(peer: A2APeer, id: string, origin: string) {
  const task = await loadTaskRow(peer, id);
  const run = await fetchLatestRun(task.id);
  return toA2ATask(task, run, origin, !peer.require_plan_approval);
}

async function handleTasksGet(peer: A2APeer, params: unknown, origin: string) {
  const p = asRecord(params);
  const id = p.id ?? p.taskId;
  if (typeof id !== 'string' && typeof id !== 'number') {
    throw new RpcError(RPC.INVALID_PARAMS, 'params.id is required');
  }
  return loadTask(peer, String(id), origin);
}

async function handleTasksList(peer: A2APeer, params: unknown, origin: string) {
  const p = asRecord(params);
  const limit = Math.min(
    Math.max(typeof p.limit === 'number' ? p.limit : 25, 1),
    100,
  );
  const offset = Math.max(typeof p.offset === 'number' ? p.offset : 0, 0);

  const res = await query<TaskRow>(
    `SELECT ${TASK_SELECT_COLUMNS}
       FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id
      WHERE t.created_by = $1
      ORDER BY t.created_at DESC
      LIMIT $2 OFFSET $3`,
    [peerCreatedBy(peer), limit, offset],
  );

  // Artifacts need a per-task run lookup; list is a summary view, so states are
  // computed from the task row alone and full detail is left to tasks/get.
  const tasks = res.rows.map((t) => ({
    kind: 'task' as const,
    id: String(t.id),
    contextId: t.repo_id !== null ? `repo-${t.repo_id}` : 'no-repo',
    status: {
      state: toA2AState(t),
      timestamp: new Date(t.updated_at).toISOString(),
    },
    metadata: {
      taskKey: t.task_key,
      taskType: t.task_type,
      devserverStatus: t.status,
      repoName: t.repo_name ?? null,
      title: t.title,
    },
  }));

  return { tasks, count: tasks.length, offset };
}

async function handleTasksCancel(peer: A2APeer, params: unknown, origin: string) {
  const p = asRecord(params);
  const id = p.id ?? p.taskId;
  if (typeof id !== 'string' && typeof id !== 'number') {
    throw new RpcError(RPC.INVALID_PARAMS, 'params.id is required');
  }

  const task = await loadTaskRow(peer, String(id));
  if (A2A_TERMINAL_STATES.has(toA2AState(task))) {
    throw new RpcError(
      RPC.TASK_NOT_CANCELABLE,
      `task is already in a terminal state (${task.status})`,
    );
  }

  // Same path as the dashboard's cancel (lib/task-cancel.ts): drop the queued
  // job so it never starts, and have the worker kill a running agent. A bare
  // status flip would leave the CLI running and spending tokens.
  const queued = await query<{ queue_job_id: string | null }>(
    `SELECT queue_job_id FROM tasks WHERE id = $1`,
    [task.id],
  );
  const outcome = await cancelTaskFully(task.id, queued.rows[0]?.queue_job_id ?? null);
  if (!outcome.ok) {
    console.error(
      `A2A tasks/cancel worker error [peer=${peer.slug} task=${task.id}]:`,
      outcome.res.status,
      outcome.body,
    );
    throw new RpcError(RPC.INTERNAL_ERROR, 'cancellation failed; try again');
  }
  if (!outcome.worker_reachable) {
    // Worker down: no agent to kill, but close out in-flight runs so the task
    // does not look perpetually mid-attempt.
    await query(
      `UPDATE task_runs SET status = 'failed', finished_at = NOW(),
              error_log = COALESCE(error_log, '') || $2
        WHERE task_id = $1 AND status IN ('started', 'verifying')`,
      [task.id, `\nCancelled by A2A peer ${peer.slug}`],
    );
  }
  await query(
    `INSERT INTO task_events (task_id, event_type, payload)
     VALUES ($1, 'a2a_task_cancelled', CAST($2 AS JSONB))`,
    [task.id, JSON.stringify({ peer: peer.slug, by: peerCreatedBy(peer) })],
  ).catch(() => {});

  return loadTask(peer, String(task.id), origin);
}

// ─── Dispatcher ──────────────────────────────────────────────────────────────

const STREAMING_METHODS = new Set([
  'message/stream',
  'tasks/subscribe',
  'tasks/resubscribe',
]);

/**
 * Streaming methods return `text/event-stream`, not a JSON body, so they are
 * dispatched before the JSON switch.
 *
 * `message/stream` first creates the task exactly as `message/send` would,
 * then follows it; `tasks/subscribe` / `tasks/resubscribe` attach to one that
 * already exists.
 */
async function dispatchStreaming(
  peer: A2APeer,
  body: JsonRpcRequest,
  origin: string,
  signal: AbortSignal,
): Promise<Response> {
  const p = asRecord(body.params);

  if (body.method === 'message/stream') {
    // Check the per-peer stream cap *before* creating a task, so a refused
    // stream does not leave behind a task (and a spent quota unit).
    if (streamCapReached(peer.id)) {
      throw new RpcError(
        RPC.UNSUPPORTED_OPERATION,
        'too many open streams for this peer; close one or use message/send',
      );
    }
    const created = await handleMessageSend(peer, body.params, origin);
    return streamTask(peer, parseInt(created.id, 10), body.id, origin, signal);
  }

  const raw = p.id ?? p.taskId;
  if (typeof raw !== 'string' && typeof raw !== 'number') {
    throw new RpcError(RPC.INVALID_PARAMS, 'params.id is required');
  }
  // Authorize before opening the stream so an unknown id fails loudly as JSON
  // rather than as an empty event stream.
  const task = await loadTaskRow(peer, String(raw));
  return streamTask(peer, task.id, body.id, origin, signal);
}

export async function POST(request: NextRequest) {
  // Authentication is transport-level in A2A: failures are HTTP statuses, not
  // JSON-RPC error objects.
  const auth = await authenticatePeer(request);
  if (!auth.ok) {
    return NextResponse.json(
      { error: auth.error },
      {
        status: auth.status,
        headers:
          auth.status === 401
            ? { 'WWW-Authenticate': 'Bearer realm="devserver-a2a"' }
            : undefined,
      },
    );
  }
  const peer = auth.peer;
  // DEVSERVER_PUBLIC_URL when set; otherwise the request's own origin. Peer
  // responses are per-caller (bearer-authenticated) and never shared-cached,
  // so a header-derived origin only ever reaches the peer that sent it.
  const { origin } = publicOrigin(request);

  let body: JsonRpcRequest;
  try {
    body = (await request.json()) as JsonRpcRequest;
  } catch {
    return NextResponse.json(rpcError(null, RPC.PARSE_ERROR, 'Parse error'));
  }

  if (Array.isArray(body)) {
    return NextResponse.json(
      rpcError(null, RPC.INVALID_REQUEST, 'Batch requests are not supported'),
    );
  }
  if (!body || body.jsonrpc !== '2.0' || typeof body.method !== 'string') {
    return NextResponse.json(
      rpcError(body?.id, RPC.INVALID_REQUEST, 'Invalid JSON-RPC 2.0 request'),
    );
  }

  try {
    if (STREAMING_METHODS.has(body.method)) {
      return await dispatchStreaming(peer, body, origin, request.signal);
    }

    let result: unknown;
    switch (body.method) {
      case 'message/send':
        result = await handleMessageSend(peer, body.params, origin);
        break;
      case 'tasks/get':
        result = await handleTasksGet(peer, body.params, origin);
        break;
      case 'tasks/list':
        result = await handleTasksList(peer, body.params, origin);
        break;
      case 'tasks/cancel':
        result = await handleTasksCancel(peer, body.params, origin);
        break;
      case 'agent/getExtendedAgentCard':
      case 'agent/getAuthenticatedExtendedCard':
        result = await buildExtendedCard(origin, peer);
        break;
      case 'tasks/pushNotificationConfig/create':
      case 'tasks/pushNotificationConfig/set':
        result = await createPushConfig(
          peer.id,
          await pushTaskId(peer, body.params),
          body.params,
        );
        break;
      case 'tasks/pushNotificationConfig/get':
        result = await getPushConfig(
          await pushTaskId(peer, body.params),
          asRecord(body.params).pushNotificationConfigId,
        );
        break;
      case 'tasks/pushNotificationConfig/list':
        result = await listPushConfigs(await pushTaskId(peer, body.params));
        break;
      case 'tasks/pushNotificationConfig/delete':
        result = await deletePushConfig(
          await pushTaskId(peer, body.params),
          asRecord(body.params).pushNotificationConfigId,
        );
        break;
      default:
        return NextResponse.json(
          rpcError(body.id, RPC.METHOD_NOT_FOUND, `Method not found: ${body.method}`),
        );
    }
    return NextResponse.json(rpcResult(body.id, result));
  } catch (err) {
    if (err instanceof RpcError) {
      return NextResponse.json(rpcError(body.id, err.code, err.message, err.data));
    }
    // Log server-side, but never leak internals (SQL text, stack traces, host
    // paths) to an external caller.
    console.error(`A2A ${body.method} error [peer=${peer.slug}]:`, err);
    return NextResponse.json(rpcError(body.id, RPC.INTERNAL_ERROR, 'Internal error'));
  }
}
