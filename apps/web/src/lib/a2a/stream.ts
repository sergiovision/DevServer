/**
 * A2A streaming (`message/stream`, `tasks/subscribe`, `tasks/resubscribe`).
 *
 * The first Server-Sent Events surface in DevServer — before this, live task
 * progress reached only the dashboard, through the WebSocket in
 * `apps/web/server.ts`.
 *
 * Implemented as a **cursor poll over the task row**, deliberately *not* by
 * reusing `server.ts`'s shared `pg.Client` LISTEN:
 *
 *   - No shared mutable state, so a stream cannot be starved or crashed by
 *     another subscriber.
 *   - Reconnection is free: a client that drops just re-subscribes and gets a
 *     fresh snapshot as the first frame.
 *   - ~1s of latency is irrelevant for tasks that run for minutes to hours.
 *
 * Each SSE frame carries a complete JSON-RPC response envelope, echoing the
 * request id, per the A2A JSON-RPC binding.
 */

import { query } from '@/lib/db';
import type { A2APeer } from './auth';
import { peerCreatedBy } from './auth';
import {
  TASK_SELECT_COLUMNS,
  TaskRow,
  buildArtifacts,
  fetchLatestRun,
  toA2AState,
  toA2ATask,
} from './mapping';
import { A2A_TERMINAL_STATES, A2ATaskState, RPC, RpcError, rpcResult } from './protocol';

const POLL_INTERVAL_MS = 1000;
/**
 * Hard cap on a single stream. A DevServer task can legitimately run for
 * hours, but an unbounded HTTP response is a resource leak when clients vanish
 * without closing. On expiry we simply end the stream — an A2A client resumes
 * with `tasks/resubscribe`.
 */
const MAX_STREAM_MS = 30 * 60 * 1000;

/**
 * Concurrent open streams per peer (in-memory, per web process). Every stream
 * holds a poll loop and a DB query per second, so an unbounded number of
 * subscriptions from one credential is a cheap way to load the database.
 */
export const MAX_STREAMS_PER_PEER = 8;
const openStreams = new Map<number, number>();

/** Reserve a stream slot for `peerId`; false when the peer is at the cap. */
export function acquireStreamSlot(peerId: number): boolean {
  const n = openStreams.get(peerId) ?? 0;
  if (n >= MAX_STREAMS_PER_PEER) return false;
  openStreams.set(peerId, n + 1);
  return true;
}

/** True when `peerId` cannot open another stream right now. */
export function streamCapReached(peerId: number): boolean {
  return (openStreams.get(peerId) ?? 0) >= MAX_STREAMS_PER_PEER;
}

export function releaseStreamSlot(peerId: number): void {
  const n = (openStreams.get(peerId) ?? 0) - 1;
  if (n <= 0) openStreams.delete(peerId);
  else openStreams.set(peerId, n);
}

/**
 * A state at which the stream ends with `final: true`. Besides the terminal
 * states this includes `input-required`: per A2A the server closes the stream
 * when the task needs input, and the client resubscribes after replying.
 */
function endsStream(state: A2ATaskState): boolean {
  return A2A_TERMINAL_STATES.has(state) || state === 'input-required';
}

function sseFrame(rpcId: string | number | null | undefined, result: unknown): string {
  return `data: ${JSON.stringify(rpcResult(rpcId, result))}\n\n`;
}

async function loadTaskForPeer(peer: A2APeer, taskId: number): Promise<TaskRow | null> {
  const res = await query<TaskRow>(
    `SELECT ${TASK_SELECT_COLUMNS}
       FROM tasks t LEFT JOIN repos r ON r.id = t.repo_id
      WHERE t.id = $1 AND t.created_by = $2`,
    [taskId, peerCreatedBy(peer)],
  );
  return res.rows[0] ?? null;
}

/**
 * Open an SSE stream that follows one task to a terminal (or input-required)
 * state. Throws `RpcError` when the peer already has `MAX_STREAMS_PER_PEER`
 * streams open — call before committing to an SSE response.
 *
 * Frame sequence:
 *   1. the full Task (snapshot, so a late subscriber is immediately correct)
 *   2. a `status-update` on every state transition
 *   3. on terminal: one `artifact-update` per artifact, then a final
 *      `status-update` with `final: true`; on `input-required` just the final
 *      `status-update` (carrying the explanation / plan)
 */
export function streamTask(
  peer: A2APeer,
  taskId: number,
  rpcId: string | number | null | undefined,
  origin: string,
  signal: AbortSignal,
): Response {
  if (!acquireStreamSlot(peer.id)) {
    throw new RpcError(
      RPC.UNSUPPORTED_OPERATION,
      `too many open streams for this peer (max ${MAX_STREAMS_PER_PEER}); ` +
        'close one or poll with tasks/get',
    );
  }
  const encoder = new TextEncoder();
  const peerMayApprove = !peer.require_plan_approval;
  // Shared between start() and cancel(): cancel() fires when the consumer
  // goes away, after which enqueue() would throw.
  let closed = false;
  let slotReleased = false;
  const releaseSlot = () => {
    if (slotReleased) return;
    slotReleased = true;
    releaseStreamSlot(peer.id);
  };

  const stream = new ReadableStream<Uint8Array>({
    cancel() {
      closed = true;
      releaseSlot();
    },
    async start(controller) {
      const started = Date.now();

      const send = (result: unknown) => {
        if (closed) return;
        try {
          controller.enqueue(encoder.encode(sseFrame(rpcId, result)));
        } catch {
          // Consumer went away between the check and the enqueue.
          closed = true;
        }
      };
      const close = () => {
        if (closed) return;
        closed = true;
        try {
          controller.close();
        } catch {
          // already closed by the client disconnecting
        }
      };

      const onAbort = () => close();
      signal.addEventListener('abort', onAbort);

      try {
        const task = await loadTaskForPeer(peer, taskId);
        if (!task) {
          close();
          return;
        }

        const contextId = task.repo_id !== null ? `repo-${task.repo_id}` : 'no-repo';
        let run = await fetchLatestRun(task.id);

        // Frame 1: full snapshot.
        const first = toA2ATask(task, run, origin, peerMayApprove);
        send(first);
        let lastState: A2ATaskState = toA2AState(task);

        // Already finished (or waiting on input) before we attached — emit
        // artifacts if terminal, then a final status and stop.
        if (endsStream(lastState)) {
          if (A2A_TERMINAL_STATES.has(lastState)) {
            for (const artifact of buildArtifacts(task, run, origin)) {
              send({ kind: 'artifact-update', taskId: String(task.id), contextId, artifact, lastChunk: true });
            }
          }
          send({
            kind: 'status-update',
            taskId: String(task.id),
            contextId,
            status: first.status,
            final: true,
          });
          close();
          return;
        }

        while (!closed && !signal.aborted) {
          await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
          if (closed || signal.aborted) break;

          if (Date.now() - started > MAX_STREAM_MS) {
            // Cap reached: end without `final`, so the client knows to resume
            // rather than treating the task as finished.
            close();
            break;
          }

          const current = await loadTaskForPeer(peer, taskId);
          if (!current) break;

          const state = toA2AState(current);
          if (state === lastState) continue;
          lastState = state;
          run = await fetchLatestRun(current.id);

          const terminal = endsStream(state);
          if (A2A_TERMINAL_STATES.has(state)) {
            for (const artifact of buildArtifacts(current, run, origin)) {
              send({ kind: 'artifact-update', taskId: String(current.id), contextId, artifact, lastChunk: true });
            }
          }

          // Re-use the full mapper for the status message so an
          // `input-required` frame carries the same explanation and plan
          // payload a `tasks/get` would return.
          const snapshot = toA2ATask(current, run, origin, peerMayApprove);
          send({
            kind: 'status-update',
            taskId: String(current.id),
            contextId,
            status: snapshot.status,
            final: terminal,
          });

          if (terminal) {
            close();
            break;
          }
        }
      } catch (err) {
        console.error(`A2A stream error [peer=${peer.slug} task=${taskId}]:`, err);
      } finally {
        signal.removeEventListener('abort', onAbort);
        close();
        releaseSlot();
      }
    },
  });

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      // Nginx buffers SSE by default, which defeats the whole point.
      'X-Accel-Buffering': 'no',
    },
  });
}
