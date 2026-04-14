import { NextRequest, NextResponse } from 'next/server';

const WORKER_URL = process.env.WORKER_URL || 'http://localhost:8000';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function POST(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseInt(id);

  try {
    const res = await fetch(`${WORKER_URL}/internal/repos/${repoId}/refresh-git`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });

    const data = await res.json();
    if (!res.ok) {
      return NextResponse.json(
        { error: data.detail || 'Refresh failed' },
        { status: res.status },
      );
    }

    return NextResponse.json(data);
  } catch (err) {
    console.error(`POST /api/repos/${id}/refresh-git error:`, err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
