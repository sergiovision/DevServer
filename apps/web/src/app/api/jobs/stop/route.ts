import { NextRequest } from 'next/server';
import { jsonError, proxyWorker } from '@/lib/route-utils';

/**
 * POST /api/jobs/stop   body: { name: string }
 *
 * Stop a running scheduler job. A pure relay of /internal/jobs/stop; an unknown
 * (or not running) job arrives as the worker's own 404 / 409 message.
 */
export async function POST(request: NextRequest) {
  const payload = (await request.json().catch(() => null)) as { name?: unknown } | null;
  if (payload === null) return jsonError(400, 'Invalid JSON body');
  return proxyWorker('/internal/jobs/stop', { method: 'POST', body: { name: payload.name } });
}
