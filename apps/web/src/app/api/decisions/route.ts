import { NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

/** List open side-effect decision points awaiting human resolution. */
export async function GET() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/decisions`, { cache: 'no-store' });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json(
        { error: data?.detail || 'failed to list decisions' },
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
