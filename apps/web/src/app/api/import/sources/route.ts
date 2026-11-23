/**
 * GET /api/import/sources
 *
 * Proxy to the worker's /internal/import/sources — the registered external
 * import sources (Confluence, …) and whether each is configured. Pass
 * ?repo_id= to evaluate per-repo credential overrides.
 *
 * The success body is a JSON array, which `proxyWorker` (object bodies only)
 * cannot relay, so this route reads the body itself.
 */
import { NextRequest, NextResponse } from 'next/server';
import { callWorker, workerErrorMessage, jsonError, workerUnreachable } from '@/lib/route-utils';

export async function GET(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  const res = await callWorker(`/internal/import/sources${qs ? `?${qs}` : ''}`);
  if (!res) return workerUnreachable();
  const data: unknown = await res.json().catch(() => []);
  if (!res.ok) {
    const body =
      data && typeof data === 'object' && !Array.isArray(data)
        ? (data as Record<string, unknown>)
        : {};
    return jsonError(res.status, workerErrorMessage(body, res.status));
  }
  return NextResponse.json(data);
}
