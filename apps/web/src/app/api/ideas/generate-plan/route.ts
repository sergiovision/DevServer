import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function POST(req: NextRequest) {
  try {
    const { project_name, description } = await req.json();
    if (!project_name?.trim() || !description?.trim()) {
      return NextResponse.json(
        { error: 'project_name and description are required' },
        { status: 400 },
      );
    }

    const res = await fetch(`${WORKER_URL}/internal/generate-plan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name, description }),
    });

    if (!res.ok) {
      const data = await res.json().catch(() => null);
      const msg = data?.detail || 'devplan skill failed';
      return NextResponse.json({ error: msg }, { status: res.status });
    }

    const plan = await res.json();
    return NextResponse.json(plan);
  } catch {
    return NextResponse.json(
      { error: 'devplan skill failed or not available' },
      { status: 500 },
    );
  }
}
