import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

/**
 * Expand one Goal Graph node one level. Proxies to the worker's
 * POST /internal/goals/{node_id}/expand, which runs the recursive
 * decomposer (atomicity check + plan-sketch).
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const nodeId = parseInt(id, 10);
  if (!Number.isInteger(nodeId)) {
    return NextResponse.json({ error: 'Invalid id' }, { status: 400 });
  }

  const body = await request.json().catch(() => ({}));

  try {
    const res = await fetch(`${WORKER_URL}/internal/goals/${nodeId}/expand`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        max_depth: body?.max_depth ?? null,
        enqueue: body?.enqueue ?? false,
      }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json(
        { error: data?.detail || 'expand failed' },
        { status: res.status },
      );
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json(
      { error: 'worker unreachable — is the worker running?' },
      { status: 502 },
    );
  }
}
