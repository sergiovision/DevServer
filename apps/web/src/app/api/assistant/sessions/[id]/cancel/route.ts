import { NextRequest, NextResponse } from 'next/server';
import { workerHeaders } from '@/lib/assistant-proxy';
import { callWorker, type RouteContext } from '@/lib/route-utils';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * Drop a conversation's worker-side session ("New chat").
 *
 * Without this the next question would continue the previous worker-side
 * session, so a "new" chat would still carry the old one's context.
 *
 * Best-effort by design: the session is an in-memory registry with a TTL, so
 * failing to clear it costs an expired entry, not correctness. The UI must
 * never block or show an error on it — it always returns 200.
 */
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  // `[id]` is the client's opaque conversation id, so it is only URL-encoded.
  // The browser's signal replaces callWorker's default timeout: closing the
  // panel aborts the call.
  const res = await callWorker(
    `/internal/assistant/sessions/${encodeURIComponent(id)}/cancel`,
    { method: 'POST', headers: workerHeaders(), signal: request.signal },
  );
  // Worker down, or the user closed the panel mid-request (`res` is null), or
  // an unreadable body. Either way the local transcript has already been
  // cleared; nothing to report.
  if (!res || !res.ok) return NextResponse.json({ ok: false, existed: false });
  const body: unknown = await res.json().catch(() => null);
  return NextResponse.json(body ?? { ok: false, existed: false });
}
