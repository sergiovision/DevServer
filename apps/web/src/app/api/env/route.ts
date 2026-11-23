import { NextRequest } from 'next/server';
import { jsonError, proxyWorker } from '@/lib/route-utils';

/**
 * GET /api/env   → `{ variables: [{ key, value, ... }] }`
 * PUT /api/env   body: `{ variables: { KEY: value } }`
 *
 * The Settings env editor and Setup wizard read and write the worker's `.env`
 * (`routes/env_config.py`). Pure relays: a failed write forwards the worker's
 * explanation (e.g. which path is not writable) with its status.
 */

export async function GET() {
  return proxyWorker('/internal/env');
}

export async function PUT(request: NextRequest) {
  const body: unknown = await request.json().catch(() => null);
  if (body === null) return jsonError(400, 'Invalid JSON body');
  return proxyWorker('/internal/env', { method: 'PUT', body });
}
