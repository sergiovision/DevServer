import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';
import { workerHeaders } from '@/lib/assistant-proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

interface RouteContext {
  params: Promise<{ id: string }>;
}

/**
 * Drop a conversation's worker-side session ("New chat").
 *
 * Without this the next question would `--resume` the previous agent CLI
 * conversation, so a "new" chat would still carry the old one's context.
 *
 * Best-effort by design: the session is an in-memory registry with a TTL, so
 * failing to clear it costs an expired entry, not correctness. The UI must
 * never block or show an error on it — it always returns 200.
 */
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  try {
    const res = await fetch(
      `${WORKER_URL}/internal/assistant/sessions/${encodeURIComponent(id)}/cancel`,
      { method: 'POST', headers: workerHeaders(), cache: 'no-store', signal: request.signal },
    );
    if (!res.ok) return NextResponse.json({ ok: false, existed: false });
    return NextResponse.json(await res.json());
  } catch {
    // Worker down, or the user closed the panel mid-request. Either way the
    // local transcript has already been cleared; nothing to report.
    return NextResponse.json({ ok: false, existed: false });
  }
}
