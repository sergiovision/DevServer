import { NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * Which system LLM answers the Ask Agent panel, and which of that vendor's
 * models the panel's picker may offer. The panel renders its badge from this.
 *
 * Degrades to a minimal answer rather than an error: a dead worker should
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
      vendor: null,
      model: null,
      workerUnreachable: true,
    });
  }
}
