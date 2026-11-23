import { NextRequest, NextResponse } from 'next/server';
import { callWorker, type RouteContext } from '@/lib/route-utils';

/**
 * GET /api/task-log/<key>
 *
 * The last 80 lines of a task's log, by task key (not a numeric id, so it is
 * only URL-encoded). Best-effort by design: a missing log or an unreachable
 * worker answers `{ lines: [] }` so the live log pane just stays empty.
 */
export async function GET(_req: NextRequest, { params }: RouteContext<{ key: string }>) {
  const { key } = await params;
  const res = await callWorker(`/internal/tasks/${encodeURIComponent(key)}/log?lines=80`);
  if (!res || !res.ok) return NextResponse.json({ lines: [] });
  const data: unknown = await res.json().catch(() => null);
  return NextResponse.json(data ?? { lines: [] });
}
