import { NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/jobs`, { cache: 'no-store' });
    if (!res.ok) {
      return NextResponse.json({ error: 'Failed to fetch jobs' }, { status: res.status });
    }
    const jobs = await res.json();
    return NextResponse.json(jobs);
  } catch (err) {
    console.error('GET /api/jobs error:', err);
    return NextResponse.json({ error: 'Worker unreachable' }, { status: 502 });
  }
}
