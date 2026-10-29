import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { decidePlan } from '@/lib/task-steering';
import { parseRouteId } from '@/lib/route-params';

interface RouteContext {
  params: Promise<{ id: string }>;
}

type Decision = 'approve' | 'reject';

function isDecision(value: unknown): value is Decision {
  return value === 'approve' || value === 'reject';
}

/**
 * POST /api/tasks/<id>/plan   body: { decision: 'approve' | 'reject' }
 *
 * Resolves an interactive task's plan gate. The worker polls the task row for
 * the decision, so recording it is all that is needed.
 */
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) {
    return NextResponse.json({ error: 'Invalid task id' }, { status: 400 });
  }

  const body: unknown = await request.json().catch(() => null);
  const decision = (body as { decision?: unknown } | null)?.decision;
  if (!isDecision(decision)) {
    return NextResponse.json(
      { error: "decision must be 'approve' or 'reject'" },
      { status: 400 },
    );
  }

  try {
    const result = await decidePlan(taskId, decision);
    if (!result.ok) {
      return NextResponse.json({ error: result.error }, { status: result.status });
    }
    return NextResponse.json({ decision: result.action });
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/plan`);
  }
}
