import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function GET() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/env`);
    const data = await res.json();
    return NextResponse.json(data);
  } catch (err) {
    console.error('GET /api/env error:', err);
    return NextResponse.json({ error: 'Failed to fetch env config' }, { status: 502 });
  }
}

export async function PUT(request: NextRequest) {
  try {
    const body = await request.json();
    const res = await fetch(`${WORKER_URL}/internal/env`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) return NextResponse.json(data, { status: res.status });
    return NextResponse.json(data);
  } catch (err) {
    console.error('PUT /api/env error:', err);
    return NextResponse.json({ error: 'Failed to update env config' }, { status: 502 });
  }
}
