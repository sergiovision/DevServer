import { NextRequest } from 'next/server';
import { proxyWorker } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

/**
 * GET  /api/schedules   → `{ schedules: [...] }`
 * POST /api/schedules   → `{ id }` with 201
 *
 * Scheduled jobs live on the worker (its asyncio scheduler owns them), so both
 * methods are pure relays of /internal/schedules.
 */

/** List schedules. */
export async function GET() {
  return proxyWorker('/internal/schedules');
}

/** Create a schedule. */
export async function POST(request: NextRequest) {
  const body: unknown = await request.json().catch(() => ({}));
  return proxyWorker('/internal/schedules', { method: 'POST', body, successStatus: 201 });
}
