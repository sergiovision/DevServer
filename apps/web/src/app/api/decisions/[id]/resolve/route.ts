import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const dynamic = 'force-dynamic';

/**
 * Approve / reject / edit an open decision point. Proxies to
 * POST /internal/decisions/{id}/resolve, which resolves the gate and
 * re-enqueues the task (resuming the agent past the gate).
 */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const decisionId = parseInt(id, 10);
  if (!Number.isInteger(decisionId)) {
    return NextResponse.json({ error: 'Invalid id' }, { status: 400 });
  }

  const body = await request.json().catch(() => ({}));
  const decision = body?.decision;
  if (!['approve', 'reject', 'edit'].includes(decision)) {
    return NextResponse.json(
      { error: "decision must be 'approve', 'reject', or 'edit'" },
      { status: 400 },
    );
  }

  try {
    const res = await fetch(`${WORKER_URL}/internal/decisions/${decisionId}/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        decision,
        comment: body?.comment ?? '',
        edited_payload: body?.edited_payload ?? null,
        resolved_by: body?.resolved_by ?? 'operator',
      }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      return NextResponse.json(
        { error: data?.detail || 'resolve failed' },
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
