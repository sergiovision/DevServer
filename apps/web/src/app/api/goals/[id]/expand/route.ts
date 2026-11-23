import { NextRequest } from 'next/server';
import { invalidId, parseRouteId, proxyWorker, type RouteContext } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

// The decomposer calls the system LLM, often more than once per level.
const EXPAND_TIMEOUT_MS = 300_000;

/**
 * POST /api/goals/<id>/expand   body: { max_depth?, enqueue? }
 *
 * Expand one Goal Graph node one level. Proxies to the worker's
 * POST /internal/goals/{node_id}/expand, which runs the recursive
 * decomposer (atomicity check + plan-sketch).
 */
export async function POST(request: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  const nodeId = parseRouteId(id);
  if (nodeId === null) return invalidId();

  const body = await request.json().catch(() => ({}));

  return proxyWorker(`/internal/goals/${nodeId}/expand`, {
    method: 'POST',
    body: {
      max_depth: body?.max_depth ?? null,
      enqueue: body?.enqueue ?? false,
    },
    timeoutMs: EXPAND_TIMEOUT_MS,
  });
}
