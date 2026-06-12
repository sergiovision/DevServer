import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

/** List schedules. */
export async function GET() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/schedules`, { cache: 'no-store' });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json({ error: data?.detail || 'failed' }, { status: res.status });
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}

/** Create a schedule. */
export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => ({}));
  try {
    const res = await fetch(`${WORKER_URL}/internal/schedules`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json({ error: data?.detail || 'create failed' }, { status: res.status });
    }
    return NextResponse.json(data, { status: 201 });
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}
