/**
 * GET /api/import/confluence/scopes?repo_id=
 *
 * Proxy to the worker's /internal/import/confluence/scopes — the Confluence
 * spaces available for filtering search results.
 */
import { NextRequest } from 'next/server';
import { proxyWorker } from '@/lib/route-utils';

// The worker lists the spaces from Confluence before answering.
const CONFLUENCE_TIMEOUT_MS = 60_000;

export async function GET(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  return proxyWorker(`/internal/import/confluence/scopes${qs ? `?${qs}` : ''}`, {
    timeoutMs: CONFLUENCE_TIMEOUT_MS,
  });
}
