import { NextRequest } from 'next/server';
import {
  invalidId,
  jsonError,
  parseRouteId,
  proxyWorker,
  type RouteContext,
} from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

/**
 * POST /api/decisions/<id>/resolve   body: { decision, comment?, edited_payload?, resolved_by? }
 *
 * Approve / reject / edit an open decision point. Proxies to
 * POST /internal/decisions/{id}/resolve, which resolves the gate and
 * re-enqueues the task (resuming the agent past the gate).
 */
export async function POST(request: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  const decisionId = parseRouteId(id);
  if (decisionId === null) return invalidId();

  const body = await request.json().catch(() => ({}));
  const decision = body?.decision;
  if (!['approve', 'reject', 'edit'].includes(decision)) {
    return jsonError(400, "decision must be 'approve', 'reject', or 'edit'");
  }

  return proxyWorker(`/internal/decisions/${decisionId}/resolve`, {
    method: 'POST',
    body: {
      decision,
      comment: body?.comment ?? '',
      edited_payload: body?.edited_payload ?? null,
      resolved_by: body?.resolved_by ?? 'operator',
    },
  });
}
