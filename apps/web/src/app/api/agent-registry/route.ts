import { NextResponse } from 'next/server';
import { fetchAgentRegistry } from '@/lib/agent-vendors';

export const dynamic = 'force-dynamic';

/**
 * GET /api/agent-registry
 *
 * The worker's vendor/model registry, relayed for client components (the
 * browser cannot reach the worker directly). Cached server-side for a minute.
 */
export async function GET() {
  try {
    return NextResponse.json(await fetchAgentRegistry());
  } catch (err) {
    console.error('GET /api/agent-registry:', err);
    return NextResponse.json({ error: 'worker unreachable' }, { status: 502 });
  }
}
