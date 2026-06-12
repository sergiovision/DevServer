import { NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

/** List skills registered in the DB. */
export async function GET() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/skills`, { cache: 'no-store' });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json({ error: data?.detail || 'failed' }, { status: res.status });
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}

/** Re-scan the skills/ directory and upsert SKILL.md folders. */
export async function POST() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/skills/sync`, { method: 'POST' });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json({ error: data?.detail || 'sync failed' }, { status: res.status });
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}
