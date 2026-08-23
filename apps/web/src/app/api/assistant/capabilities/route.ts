import { NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * What the assistant can do on this deployment — edition, system LLM, and
 * whether tools/actions are available. The panel renders its badge from this.
 *
 * Degrades to a free-tier answer rather than an error: a dead worker should
 * make the panel say "the worker is unreachable", not make it fail to open.
 */
export async function GET() {
  try {
    const res = await fetch(`${WORKER_URL}/internal/assistant/capabilities`, {
      cache: 'no-store',
    });
    if (!res.ok) throw new Error(`worker ${res.status}`);
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({
      edition: 'free',
      vendor: null,
      model: null,
      tools: false,
      actions: false,
      workerUnreachable: true,
    });
  }
}
