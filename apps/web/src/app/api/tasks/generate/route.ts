import { NextRequest } from 'next/server';
import { jsonError, proxyWorker } from '@/lib/route-utils';

/**
 * POST /api/tasks/generate   body: { description: string }
 *
 * "Fill Task": the worker's devtask skill turns a plain-text description into
 * task fields through the system LLM. A pure relay of
 * /internal/generate-task; the worker's error detail is forwarded as-is.
 */

// An LLM call (possibly through a vendor CLI in `max` mode) easily outlives
// the default proxy timeout.
const GENERATE_TIMEOUT_MS = 600_000;

export async function POST(req: NextRequest) {
  const payload = (await req.json().catch(() => null)) as { description?: unknown } | null;
  const description = payload?.description;
  if (typeof description !== 'string' || !description.trim()) {
    return jsonError(400, 'Description is required');
  }

  return proxyWorker('/internal/generate-task', {
    method: 'POST',
    body: { description },
    timeoutMs: GENERATE_TIMEOUT_MS,
  });
}
