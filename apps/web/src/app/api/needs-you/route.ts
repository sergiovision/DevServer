/**
 * GET /api/needs-you → `{ blocked, plans_awaiting_decision }`.
 *
 * Backs the sidebar's Needs You pill. It replaces a client-side
 * `/api/tasks?status=blocked&limit=200` fetch that shipped whole task rows to
 * count them, capped at 200, and missed plans waiting for approval.
 *
 * Thin shell: the SQL narrows to candidate rows and fetches only the columns
 * the rule reads; which rows count is decided by the pure
 * `countTasksNeedingOperator` in `lib/task-steering.ts`.
 */

import { NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import {
  countTasksNeedingOperator,
  type OperatorAttentionRow,
} from '@/lib/task-steering';

export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    const result = await query<OperatorAttentionRow>(
      `SELECT t.status, t.mode, t.plan_approved_at, t.plan_rejected_at,
              (t.mode = 'interactive' AND EXISTS (
                 SELECT 1 FROM task_runs tr
                  WHERE tr.task_id = t.id AND tr.plan_json IS NOT NULL
              )) AS has_plan
         FROM tasks t
        WHERE t.status = 'blocked'
           OR (t.status IN ('running', 'verifying')
               AND t.mode = 'interactive'
               AND t.plan_approved_at IS NULL
               AND t.plan_rejected_at IS NULL)`,
    );
    return NextResponse.json(countTasksNeedingOperator(result.rows));
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/needs-you');
  }
}
