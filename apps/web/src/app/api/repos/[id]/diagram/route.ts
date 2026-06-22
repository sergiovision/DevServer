import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';

const WORKER_URL = process.env.WORKER_URL || 'http://localhost:8000';

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseInt(id);

  try {
    const res = await fetch(`${WORKER_URL}/internal/repos/${repoId}/diagram`);
    const data = await res.json();
    if (!res.ok) {
      return NextResponse.json(
        { error: data.detail || 'Diagram generation failed' },
        { status: res.status },
      );
    }
    return NextResponse.json(data);
  } catch (err) {
    return apiErrorResponse(err, `GET /api/repos/${id}/diagram`);
  }
}
