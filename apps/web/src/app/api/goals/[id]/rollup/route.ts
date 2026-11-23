import { NextRequest } from 'next/server';
import { invalidId, parseRouteId, proxyWorker, type RouteContext } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

// Summarising and scoring the children goes through the system LLM.
const ROLLUP_TIMEOUT_MS = 300_000;

/**
 * POST /api/goals/<id>/rollup
 *
 * Roll up a Goal Graph node's completed children into a parent summary +
 * 0–100 evaluator score. Proxies to POST /internal/goals/{node_id}/rollup.
 */
export async function POST(_request: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  const nodeId = parseRouteId(id);
  if (nodeId === null) return invalidId();

  return proxyWorker(`/internal/goals/${nodeId}/rollup`, {
    method: 'POST',
    timeoutMs: ROLLUP_TIMEOUT_MS,
  });
}
