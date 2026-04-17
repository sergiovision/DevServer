import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { WORKER_URL } from '@/lib/worker-url';

/**
 * POST /api/tasks/<id>/compact
 *
 * Trigger context compaction for a task. Delegates to the worker's
 * /internal/tasks/<key>/compact endpoint, which summarises the
 * transcript through the system LLM, writes the summary to
 * tasks.compacted_context, and emits a context_compacted event.
 */

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);
  try {
    const body = (await request.json().catch(() => ({}))) as {
      reason?: string;
    };

    const row = await query<{ task_key: string }>(
      `SELECT task_key FROM tasks WHERE id = $1`,
      [taskId],
    );
    if (row.rows.length === 0) {
      return NextResponse.json({ error: 'task not found' }, { status: 404 });
    }
    const taskKey = row.rows[0].task_key;

    const workerRes = await fetch(
      `${WORKER_URL}/internal/tasks/${encodeURIComponent(taskKey)}/compact?reason=${
        encodeURIComponent(body.reason || 'manual')
      }`,
      { method: 'POST' },
    );
    if (!workerRes.ok) {
      const detail = await workerRes.json().catch(() => ({}));
      return NextResponse.json(
        { error: detail.detail || 'compaction failed' },
        { status: workerRes.status },
      );
    }
    const out = await workerRes.json();
    return NextResponse.json(out);
  } catch (err) {
    console.error(`POST /api/tasks/${id}/compact error:`, err);
    return NextResponse.json(
      { error: 'internal server error' },
      { status: 500 },
    );
  }
}
