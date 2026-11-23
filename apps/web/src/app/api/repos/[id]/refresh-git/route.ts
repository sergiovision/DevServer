import { NextRequest } from 'next/server';
import { invalidId, parseRouteId, proxyWorker, type RouteContext } from '@/lib/route-utils';

/**
 * POST /api/repos/<id>/refresh-git
 *
 * Clone or fetch the repo's bare repo and worktree (for a local repo: validate
 * the Local Root Folder). A pure relay of /internal/repos/{id}/refresh-git;
 * a git failure arrives as the worker's 500 with git's message.
 */

// A first clone of a large repository takes minutes, not seconds.
const REFRESH_TIMEOUT_MS = 600_000;

export async function POST(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseRouteId(id);
  if (repoId === null) return invalidId('repo id');

  return proxyWorker(`/internal/repos/${repoId}/refresh-git`, {
    method: 'POST',
    timeoutMs: REFRESH_TIMEOUT_MS,
  });
}
