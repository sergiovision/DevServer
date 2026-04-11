import { NextRequest, NextResponse } from 'next/server';

import { WORKER_URL } from '@/lib/worker-url';

export async function GET() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/night-cycle/status`, {
      signal: AbortSignal.timeout(3000),
    });
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({ active: false });
  }
}

export async function POST(request: NextRequest) {
  const body = await request.json();
  const { action, end_hour } = body;

  const endpoint = action === 'stop' ? 'stop' : 'start';

  try {
    const res = await fetch(`${WORKER_URL}/internal/night-cycle/${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ end_hour: end_hour ?? 7 }),
      signal: AbortSignal.timeout(10000),
    });
    return NextResponse.json(await res.json());
  } catch (err) {
    return NextResponse.json({ error: String(err) }, { status: 500 });
  }
}
