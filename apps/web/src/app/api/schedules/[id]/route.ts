import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

async function proxy(method: string, id: string, body?: unknown) {
  const nid = parseInt(id, 10);
  if (!Number.isInteger(nid)) {
    return NextResponse.json({ error: 'Invalid id' }, { status: 400 });
  }
  try {
    const res = await fetch(`${WORKER_URL}/internal/schedules/${nid}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json({ error: data?.detail || `${method} failed` }, { status: res.status });
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const body = await request.json().catch(() => ({}));
  return proxy('PATCH', id, body);
}

export async function DELETE(_request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return proxy('DELETE', id);
}
