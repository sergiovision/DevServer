import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

/**
 * Roll up a Goal Graph node's completed children into a parent summary +
 * 0–100 evaluator score. Proxies to POST /internal/goals/{node_id}/rollup.
 */
export async function POST(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const nodeId = parseInt(id, 10);
  if (!Number.isInteger(nodeId)) {
    return NextResponse.json({ error: 'Invalid id' }, { status: 400 });
  }

  try {
    const res = await fetch(`${WORKER_URL}/internal/goals/${nodeId}/rollup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json(
        { error: data?.detail || 'rollup not ready' },
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
