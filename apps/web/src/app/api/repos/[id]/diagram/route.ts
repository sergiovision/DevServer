import { NextRequest } from 'next/server';
import { invalidId, parseRouteId, proxyWorker, type RouteContext } from '@/lib/route-utils';

/**
 * GET /api/repos/<id>/diagram
 *
 * Mermaid module-tree diagram of the repo (`{ repo_id, mermaid, stats }`).
 * A pure relay of the worker's /internal/repos/{id}/diagram, which builds it
 * deterministically from the repo map; 409 means the worktree is missing.
 */

// Walking a large worktree for the repo map can outlast the default timeout.
const DIAGRAM_TIMEOUT_MS = 120_000;

export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const repoId = parseRouteId(id);
  if (repoId === null) return invalidId('repo id');

  return proxyWorker(`/internal/repos/${repoId}/diagram`, { timeoutMs: DIAGRAM_TIMEOUT_MS });
}
