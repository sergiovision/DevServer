import { NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function POST() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/env/apply`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) return NextResponse.json(data, { status: res.status });
    return NextResponse.json(data);
  } catch (err) {
    console.error('POST /api/env/apply error:', err);
    return NextResponse.json({ error: 'Failed to apply config' }, { status: 502 });
  }
}
