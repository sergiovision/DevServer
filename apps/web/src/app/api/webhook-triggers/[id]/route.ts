import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { invalidId, jsonError, parseRouteId, type RouteContext } from '@/lib/route-utils';
import { parseTriggerUpdate } from '@/lib/webhooks/trigger-input';
import { deleteTrigger, updateTrigger } from '@/lib/webhooks/triggers';

/**
 * PATCH / DELETE /api/webhook-triggers/<id>
 *
 * Admin edits of one webhook trigger. Never returns the signing secret; the
 * store helpers in lib/webhooks/triggers.ts own the SQL.
 */

/** PATCH /api/webhook-triggers/<id> — update any editable field (e.g. `enabled`). */
export async function PATCH(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const triggerId = parseRouteId(id);
  if (triggerId === null) return invalidId('trigger id');

  const body: unknown = await request.json().catch(() => null);
  const input = parseTriggerUpdate(body);
  if (!input.ok) {
    return jsonError(400, input.error);
  }

  try {
    const trigger = await updateTrigger(triggerId, input.values);
    if (!trigger) {
      return jsonError(404, 'Trigger not found');
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
  if (triggerId === null) return invalidId('trigger id');

  try {
    const deleted = await deleteTrigger(triggerId);
    if (!deleted) {
      return jsonError(404, 'Trigger not found');
    }
    return NextResponse.json({ success: true });
  } catch (err) {
    return apiErrorResponse(err, `DELETE /api/webhook-triggers/${id}`);
  }
}
