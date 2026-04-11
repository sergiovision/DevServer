/**
 * GET /api/task-patches/[key]
 *
 * Proxy to the worker's /internal/tasks/{task_key}/patches endpoint.
 * Returns the current patch set for a task (list of files, stats). Does
 * NOT trigger regeneration — that's what POST does.
 */
import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

interface RouteContext {
  params: Promise<{ key: string }>;
}

export async function GET(_req: NextRequest, { params }: RouteContext) {
  const { key } = await params;
  try {
    const res = await fetch(
      `${WORKER_URL}/internal/tasks/${encodeURIComponent(key)}/patches`,
      { cache: 'no-store' },
    );
    if (!res.ok) {
      return NextResponse.json(
        { ok: false, task_key: key, files: [] },
        { status: res.status },
      );
    }
    const data = await res.json();
    return NextResponse.json(data);
  } catch {
    return NextResponse.json(
      { ok: false, task_key: key, files: [], error: 'worker unreachable' },
      { status: 502 },
    );
  }
}

/**
 * POST /api/task-patches/[key]
 *
 * Regenerate patches on demand. Proxies to the worker's
 * /internal/tasks/{task_key}/patches/generate endpoint. Safe to call
 * multiple times — the worker wipes and rebuilds the patches directory.
 */
export async function POST(_req: NextRequest, { params }: RouteContext) {
  const { key } = await params;
  try {
    const res = await fetch(
      `${WORKER_URL}/internal/tasks/${encodeURIComponent(key)}/patches/generate`,
      { method: 'POST', cache: 'no-store' },
    );
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      return NextResponse.json(
        { ok: false, error: data?.detail || data?.error || `worker returned ${res.status}` },
        { status: res.status },
      );
    }
    return NextResponse.json(data);
  } catch {
    return NextResponse.json(
      { ok: false, error: 'worker unreachable' },
      { status: 502 },
    );
  }
}
