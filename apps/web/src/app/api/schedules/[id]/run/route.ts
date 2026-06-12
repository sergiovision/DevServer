import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

/** Fire a schedule immediately. */
export async function POST(_request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const nid = parseInt(id, 10);
  if (!Number.isInteger(nid)) {
    return NextResponse.json({ error: 'Invalid id' }, { status: 400 });
  }
  try {
    const res = await fetch(`${WORKER_URL}/internal/schedules/${nid}/run`, { method: 'POST' });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json({ error: data?.detail || 'run failed' }, { status: res.status });
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}
