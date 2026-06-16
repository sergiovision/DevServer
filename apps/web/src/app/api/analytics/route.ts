import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const days = Math.min(parseInt(searchParams.get('days') || '30'), 90);

  try {
    // 1. Daily stats (cost, success/fail, duration, turns)
    const dailyResult = await query(
      `SELECT date, completed, failed, cost_usd,
              total_duration_ms, total_turns
       FROM daily_stats
       WHERE date >= CURRENT_DATE - $1 * INTERVAL '1 day'
       ORDER BY date ASC`,
      [days],
    );

    // 2. Per-vendor cost breakdown (from task_runs joined with tasks)
    const vendorCostResult = await query(
      `SELECT
         COALESCE(t.agent_vendor, 'anthropic') AS vendor,
         DATE(tr.started_at) AS date,
         SUM(tr.cost_usd) AS cost_usd,
         COUNT(*) AS runs,
         SUM(CASE WHEN tr.status IN ('success', 'passed') THEN 1 ELSE 0 END) AS successes,
         SUM(tr.duration_ms) AS duration_ms,
         SUM(tr.turns) AS turns
       FROM task_runs tr
       JOIN tasks t ON t.id = tr.task_id
       WHERE tr.started_at >= CURRENT_DATE - $1 * INTERVAL '1 day'
         AND tr.status != 'started'
       GROUP BY COALESCE(t.agent_vendor, 'anthropic'), DATE(tr.started_at)
       ORDER BY date ASC, vendor`,
      [days],
    );

    // 3. Totals summary.
    //    Counts/duration/turns come from the denormalized daily_stats rollup,
    //    but TOTAL COST is taken from task_runs — the same authoritative source
    //    as Task View and the per-vendor breakdown below. daily_stats.cost_usd
    //    is only written in the heavy coding-task success path, so it misses
    //    skill/research (light) tasks and failed-but-costly runs, which made the
    //    dashboard "Total Cost" read ~$0.00 while real per-task costs existed.
    const totalsResult = await query(
      `SELECT
         SUM(completed) AS total_completed,
         SUM(failed) AS total_failed,
         SUM(total_duration_ms) AS total_duration_ms,
         SUM(total_turns) AS total_turns
       FROM daily_stats
       WHERE date >= CURRENT_DATE - $1 * INTERVAL '1 day'`,
      [days],
    );

    const totalCostResult = await query(
      `SELECT COALESCE(SUM(cost_usd), 0) AS total_cost
       FROM task_runs
       WHERE started_at >= CURRENT_DATE - $1 * INTERVAL '1 day'
         AND status != 'started'`,
      [days],
    );

    // 4. Per-vendor totals
    const vendorTotalsResult = await query(
      `SELECT
         COALESCE(t.agent_vendor, 'anthropic') AS vendor,
         SUM(tr.cost_usd) AS cost_usd,
         COUNT(*) AS runs,
         SUM(CASE WHEN tr.status IN ('success', 'passed') THEN 1 ELSE 0 END) AS successes
       FROM task_runs tr
       JOIN tasks t ON t.id = tr.task_id
       WHERE tr.started_at >= CURRENT_DATE - $1 * INTERVAL '1 day'
         AND tr.status != 'started'
       GROUP BY COALESCE(t.agent_vendor, 'anthropic')
       ORDER BY cost_usd DESC`,
      [days],
    );

    const totals = totalsResult.rows[0] ?? {
      total_completed: 0,
      total_failed: 0,
      total_duration_ms: 0,
      total_turns: 0,
    };
    // Authoritative spend from task_runs (see query #3 comment).
    totals.total_cost = totalCostResult.rows[0]?.total_cost ?? 0;

    return NextResponse.json({
      days,
      daily: dailyResult.rows,
      vendor_daily: vendorCostResult.rows,
      totals,
      vendor_totals: vendorTotalsResult.rows,
    });
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/analytics');
  }
}
