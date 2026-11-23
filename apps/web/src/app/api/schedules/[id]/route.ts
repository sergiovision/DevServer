import { NextRequest, NextResponse } from 'next/server';
import { invalidId, parseRouteId, proxyWorker, type RouteContext } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

/**
 * PATCH  /api/schedules/<id>   body: the fields to change
 * DELETE /api/schedules/<id>
 *
 * Pure relays of /internal/schedules/{id}; the worker's scheduler owns the rows.
 */

function relay(method: 'PATCH' | 'DELETE', rawId: string, body?: unknown): Promise<NextResponse> {
  const scheduleId = parseRouteId(rawId);
  if (scheduleId === null) return Promise.resolve(invalidId());
  return proxyWorker(`/internal/schedules/${scheduleId}`, { method, body });
}

export async function PATCH(request: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  const body: unknown = await request.json().catch(() => ({}));
  return relay('PATCH', id, body);
}

export async function DELETE(_request: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  return relay('DELETE', id);
}
