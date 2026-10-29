import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { parseRouteId } from '@/lib/route-params';
import { parseTriggerUpdate } from '@/lib/webhooks/trigger-input';
import { deleteTrigger, updateTrigger } from '@/lib/webhooks/triggers';

interface RouteContext {
  params: Promise<{ id: string }>;
}

/** PATCH /api/webhook-triggers/<id> — update any editable field (e.g. `enabled`). */
export async function PATCH(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const triggerId = parseRouteId(id);
  if (triggerId === null) {
    return NextResponse.json({ error: 'Invalid trigger id' }, { status: 400 });
  }

  const body: unknown = await request.json().catch(() => null);
  const input = parseTriggerUpdate(body);
  if (!input.ok) {
    return NextResponse.json({ error: input.error }, { status: 400 });
  }

  try {
    const trigger = await updateTrigger(triggerId, input.values);
    if (!trigger) {
      return NextResponse.json({ error: 'Trigger not found' }, { status: 404 });
    }
    return NextResponse.json(trigger);
  } catch (err) {
    return apiErrorResponse(err, `PATCH /api/webhook-triggers/${id}`);
  }
}

/** DELETE /api/webhook-triggers/<id> */
export async function DELETE(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const triggerId = parseRouteId(id);
  if (triggerId === null) {
    return NextResponse.json({ error: 'Invalid trigger id' }, { status: 400 });
  }

  try {
    const deleted = await deleteTrigger(triggerId);
    if (!deleted) {
      return NextResponse.json({ error: 'Trigger not found' }, { status: 404 });
    }
    return NextResponse.json({ success: true });
  } catch (err) {
    return apiErrorResponse(err, `DELETE /api/webhook-triggers/${id}`);
  }
}
