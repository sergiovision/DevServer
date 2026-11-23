import { NextRequest } from 'next/server';
import { invalidId, parseRouteId, proxyWorker, type RouteContext } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

/**
 * POST /api/schedules/<id>/run
 *
 * Fire a schedule immediately. A pure relay of /internal/schedules/{id}/run.
 */
export async function POST(_request: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  const scheduleId = parseRouteId(id);
  if (scheduleId === null) return invalidId();
  return proxyWorker(`/internal/schedules/${scheduleId}/run`, { method: 'POST' });
}
