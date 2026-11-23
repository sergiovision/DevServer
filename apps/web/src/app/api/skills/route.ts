import { proxyWorker } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

/**
 * GET  /api/skills   → `{ skills: [...] }`, the skills registered in the DB
 * POST /api/skills   → re-scan the skills/ directory and upsert SKILL.md folders
 *
 * Pure relays of the worker's /internal/skills endpoints.
 */

// A sync walks the skills directory and upserts every SKILL.md it finds.
const SYNC_TIMEOUT_MS = 60_000;

/** List skills registered in the DB. */
export async function GET() {
  return proxyWorker('/internal/skills');
}

/** Re-scan the skills/ directory and upsert SKILL.md folders. */
export async function POST() {
  return proxyWorker('/internal/skills/sync', { method: 'POST', timeoutMs: SYNC_TIMEOUT_MS });
}
