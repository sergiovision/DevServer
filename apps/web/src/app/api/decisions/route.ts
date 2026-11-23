import { proxyWorker } from '@/lib/route-utils';

export const dynamic = 'force-dynamic';

/**
 * GET /api/decisions
 *
 * List open side-effect decision points awaiting human resolution
 * (`{ decisions: [...] }`). A pure relay of the worker's /internal/decisions.
 */
export async function GET() {
  return proxyWorker('/internal/decisions');
}
