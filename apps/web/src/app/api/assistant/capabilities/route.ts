import { NextResponse } from 'next/server';
import { callWorker } from '@/lib/route-utils';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * Which system LLM answers the Ask Agent panel, and which of that vendor's
 * models the panel's picker may offer, and whether tools/actions are available.
 * The panel renders its badge from this.
 *
 * Degrades to a minimal answer rather than an error: a dead worker should
 * make the panel say "the worker is unreachable", not make it fail to open.
 */
export async function GET() {
  const res = await callWorker('/internal/assistant/capabilities');
  if (res?.ok) {
    const body: unknown = await res.json().catch(() => null);
    if (body && typeof body === 'object') return NextResponse.json(body);
  }
  return NextResponse.json({
    vendor: null,
    model: null,
    tools: false,
    actions: false,
    workerUnreachable: true,
  });
}
