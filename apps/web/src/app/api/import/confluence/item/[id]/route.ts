/**
 * GET /api/import/confluence/item/[id]?repo_id=
 *
 * Proxy to the worker's /internal/import/confluence/item/{id} — one page
 * fetched and normalised to a Markdown draft (used by the preview pane).
 *
 * `[id]` is a Confluence page id, owned by Confluence rather than this
 * database, so it is URL-encoded and passed through rather than parsed.
 */
import { NextRequest } from 'next/server';
import { proxyWorker, type RouteContext } from '@/lib/route-utils';

// The worker fetches the page from Confluence before answering.
const CONFLUENCE_TIMEOUT_MS = 60_000;

export async function GET(req: NextRequest, { params }: RouteContext) {
  const { id } = await params;
  const qs = req.nextUrl.searchParams.toString();
  return proxyWorker(
    `/internal/import/confluence/item/${encodeURIComponent(id)}${qs ? `?${qs}` : ''}`,
    { timeoutMs: CONFLUENCE_TIMEOUT_MS },
  );
}
