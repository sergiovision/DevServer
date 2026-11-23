import { NextResponse } from 'next/server';
import { callWorker, jsonError, workerErrorMessage, workerUnreachable } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

/**
 * GET /api/jobs
 *
 * The worker scheduler's jobs and their state. The worker answers a JSON
 * array, which `proxyWorker` (object bodies only) cannot relay, so this route
 * reads the body itself.
 */
export async function GET() {
  const res = await callWorker('/internal/jobs');
  if (!res) return workerUnreachable();
  const data: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const body =
      data && typeof data === 'object' && !Array.isArray(data)
        ? (data as Record<string, unknown>)
        : {};
    return jsonError(res.status, workerErrorMessage(body, res.status));
  }
  return NextResponse.json(data);
}
