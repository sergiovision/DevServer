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
 * GET /api/tasks/<id>/prediction
 *
 * Outcome forecast for a task. Proxies the worker's
 * /internal/tasks/<key>/prediction endpoint. The `basis` field says where
 * the number comes from (e.g. the repo's recent success rate).
 */

// The similar-task forecast embeds the task text; the first call loads the
// embedding model on the worker, which can outlast the default timeout.
const PREDICTION_TIMEOUT_MS = 60_000;

export async function GET(_request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const taskId = parseRouteId(id);
  if (taskId === null) return invalidId('task id');
  try {
    const row = await query<{ task_key: string }>(
      `SELECT task_key FROM tasks WHERE id = $1`,
      [taskId],
    );
    if (row.rows.length === 0) {
      return jsonError(404, 'task not found');
    }
    const taskKey = row.rows[0].task_key;

    return await proxyWorker(`/internal/tasks/${encodeURIComponent(taskKey)}/prediction`, {
      timeoutMs: PREDICTION_TIMEOUT_MS,
    });
  } catch (err) {
    return apiErrorResponse(err, `GET /api/tasks/${id}/prediction`);
  }
}
