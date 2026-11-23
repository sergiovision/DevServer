import { proxyWorker } from '@/lib/route-utils';

/**
 * POST /api/env/apply
 *
 * Reload the running worker's settings from `.env`, so a saved change takes
 * effect without a restart. A pure relay of /internal/env/apply.
 */
export async function POST() {
  return proxyWorker('/internal/env/apply', { method: 'POST' });
}
