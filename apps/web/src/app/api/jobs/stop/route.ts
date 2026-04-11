import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const res = await fetch(`${WORKER_URL}/internal/jobs/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: body.name }),
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    console.error('POST /api/jobs/stop error:', err);
    return NextResponse.json({ error: 'Worker unreachable' }, { status: 502 });
  }
}
