import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { WORKER_URL } from '@/lib/worker-url';

/**
 * GET /api/tasks/<id>/prediction
 *
 * Outcome forecast for a task. Proxies the worker's
 * /internal/tasks/<key>/prediction endpoint, which returns a repo-level
 * baseline (free) or a similar-task forecast (Pro). Available in both
 * editions — the `basis` field distinguishes the source.
 */

interface RouteContext {
  params: Promise<{ id: string }>;
}

export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseInt(id);
  try {
    const row = await query<{ task_key: string }>(
      `SELECT task_key FROM tasks WHERE id = $1`,
      [taskId],
    );
    if (row.rows.length === 0) {
      return NextResponse.json({ error: 'task not found' }, { status: 404 });
    }
    const taskKey = row.rows[0].task_key;

    const workerRes = await fetch(
      `${WORKER_URL}/internal/tasks/${encodeURIComponent(taskKey)}/prediction`,
      { method: 'GET' },
    );
    if (!workerRes.ok) {
      const detail = await workerRes.json().catch(() => ({}));
      return NextResponse.json(
        { error: detail.detail || 'prediction failed' },
        { status: workerRes.status },
      );
    }
    return NextResponse.json(await workerRes.json());
  } catch (err) {
    console.error(`GET /api/tasks/${id}/prediction error:`, err);
    return NextResponse.json({ error: 'internal server error' }, { status: 500 });
  }
}
