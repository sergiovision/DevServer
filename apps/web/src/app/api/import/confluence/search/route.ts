/**
 * GET /api/import/confluence/search?q=&scope=&limit=&repo_id=
 *
 * Proxy to the worker's /internal/import/confluence/search — CQL or free-text
 * search (prefix the query with "cql:" for raw CQL).
 */
import { NextRequest } from 'next/server';
import { proxyWorker } from '@/lib/route-utils';

// The worker runs the search against Confluence before answering.
const CONFLUENCE_TIMEOUT_MS = 60_000;

export async function GET(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  return proxyWorker(`/internal/import/confluence/search${qs ? `?${qs}` : ''}`, {
    timeoutMs: CONFLUENCE_TIMEOUT_MS,
  });
}
