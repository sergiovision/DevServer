import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function POST(req: NextRequest) {
  try {
    const { description } = await req.json();
    if (!description?.trim()) {
      return NextResponse.json({ error: 'Description is required' }, { status: 400 });
    }

    const res = await fetch(`${WORKER_URL}/internal/generate-task`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description }),
    });

    if (!res.ok) {
      const data = await res.json().catch(() => null);
      const msg = data?.detail || 'devtask skill failed';
      return NextResponse.json({ error: msg }, { status: res.status });
    }

    const task = await res.json();
    return NextResponse.json(task);
  } catch {
    return NextResponse.json({ error: 'devtask skill failed or not available' }, { status: 500 });
  }
}
