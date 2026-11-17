/**
 * Shared plumbing for peer-scoped REST routes under `/api/a2a/tasks/...`
 * (currently the patch-set download handed out as an artifact URL).
 *
 * These live under `/api/a2a` — not `/api/tasks` — because they are a peer
 * surface: bearer-authenticated via `authenticatePeer` and confined to tasks
 * the peer created (`created_by` tenancy), exactly like the JSON-RPC methods.
 * The dashboard's `/api/tasks/<id>/patches` stays an operator-only route.
 */

import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { authenticatePeer, peerCreatedBy } from './auth';
import type { A2APeer } from './auth';

export type PeerTaskResult =
  | { ok: true; peer: A2APeer; taskKey: string }
  | { ok: false; response: NextResponse };

/** Authenticate the peer and resolve a task it owns, by numeric id. */
export async function resolvePeerTask(
  request: NextRequest,
  rawId: string,
): Promise<PeerTaskResult> {
  const auth = await authenticatePeer(request);
  if (!auth.ok) {
    return {
      ok: false,
      response: NextResponse.json(
        { error: auth.error },
        {
          status: auth.status,
          headers:
            auth.status === 401
              ? { 'WWW-Authenticate': 'Bearer realm="devserver-a2a"' }
              : undefined,
        },
      ),
    };
  }

  const id = Number(rawId);
  const notFound = {
    ok: false as const,
    response: NextResponse.json({ error: 'task not found' }, { status: 404 }),
  };
  if (!Number.isInteger(id) || id <= 0) return notFound;

  const res = await query<{ task_key: string }>(
    `SELECT task_key FROM tasks WHERE id = $1 AND created_by = $2`,
    [id, peerCreatedBy(auth.peer)],
  );
  if (res.rows.length === 0) return notFound;
  return { ok: true, peer: auth.peer, taskKey: res.rows[0].task_key };
}

/**
 * Map a worker error to a peer-safe response. The worker's `detail` text can
 * name host paths or internal state, so it is never relayed to an external
 * caller — only a generic message with a coarse status.
 */
export function peerWorkerError(res: Response): NextResponse {
  if (res.status === 404) {
    return NextResponse.json({ error: 'not found' }, { status: 404 });
  }
  return NextResponse.json({ error: 'patch export unavailable' }, { status: 502 });
}
