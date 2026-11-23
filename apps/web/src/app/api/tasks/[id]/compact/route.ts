import { NextRequest } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import {
  invalidId,
  jsonError,
  parseRouteId,
  proxyWorker,
  type RouteContext,
} from '@/lib/route-utils';

/**
 * POST /api/tasks/<id>/compact
 *
 * Trigger context compaction for a task. Delegates to the worker's
 * /internal/tasks/<key>/compact endpoint, which summarises the
 * transcript through the system LLM, writes the summary to
 * tasks.compacted_context, and emits a context_compacted event.
 */

// Summarising through the system LLM can take far longer than a plain
// worker call; the default proxy timeout would cut it off mid-summary.
const COMPACTION_TIMEOUT_MS = 300_000;

export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) return invalidId('task id');
  try {
    const body = (await request.json().catch(() => ({}))) as {
      reason?: string;
    };

    const row = await query<{ task_key: string }>(
      `SELECT task_key FROM tasks WHERE id = $1`,
      [taskId],
    );
    if (row.rows.length === 0) {
      return jsonError(404, 'task not found');
    }
    const taskKey = row.rows[0].task_key;

    return await proxyWorker(
      `/internal/tasks/${encodeURIComponent(taskKey)}/compact?reason=${encodeURIComponent(
        body.reason || 'manual',
      )}`,
      { method: 'POST', timeoutMs: COMPACTION_TIMEOUT_MS },
    );
  } catch (err) {
    return apiErrorResponse(err, `POST /api/tasks/${id}/compact`);
  }
}
