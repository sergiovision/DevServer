/**
 * GET /api/analytics?days=N — dashboard analytics: daily rollups, per-vendor
 * cost, governance KPIs and token metrics (throughput per vendor, tokens per
 * task). Thin shell: SQL here, metric math in `lib/analytics-metrics.ts`.
 *
 * Token metrics read per-attempt `cost_update` task_events (no schema
 * change: event payloads are free-form JSONB). Until the worker includes
 * token counts in that payload the token sections are empty and
 * `token_data_available` is false — the rest of the response is unaffected.
 */
import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import {
  computeTokensPerTask,
  computeVendorThroughput,
  type VendorTaskTokensRow,
  type VendorThroughputRow,
} from '@/lib/analytics-metrics';

/**
 * SQL for a numeric JSONB payload field that never throws: a malformed value
 * (written by an older/foreign worker) counts as 0 instead of failing the
 * whole dashboard with a cast error. `key` is always a constant below.
 */
function payloadNumber(key: string): string {
  const field = `(e.payload->>'${key}')`;
  return `CASE WHEN ${field} ~ '^[0-9]+(\\.[0-9]+)?$' THEN ${field}::numeric ELSE 0 END`;
}

// The vendor that actually ran the attempt when the payload says so (a
// backup-vendor attempt), else the task's primary vendor.
const ATTEMPT_VENDOR = "COALESCE(NULLIF(e.payload->>'vendor', ''), t.agent_vendor, 'anthropic')";

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

    // 5. Governance KPIs neither Devin nor CrewAI surface.
    //    cost-per-PR  = total spend / PRs opened (the number a buyer can't get
    //                   elsewhere); abstain savings = tasks the reality gate
    //                   refused to start (abstain_reason set) × avg PR cost,
    //                   i.e. spend avoided on runs that would have failed.
    const prCountResult = await query(
      `SELECT COUNT(*) AS pr_count
       FROM task_runs
       WHERE pr_url IS NOT NULL
         AND started_at >= CURRENT_DATE - $1 * INTERVAL '1 day'`,
      [days],
    );
    const abstainResult = await query(
      `SELECT COUNT(*) AS abstain_count
       FROM tasks
       WHERE abstain_reason IS NOT NULL
         AND updated_at >= CURRENT_DATE - $1 * INTERVAL '1 day'`,
      [days],
    );

    const totalCost = Number(totals.total_cost) || 0;
    const prCount = Number(prCountResult.rows[0]?.pr_count) || 0;
    const abstainCount = Number(abstainResult.rows[0]?.abstain_count) || 0;
    const costPerPr = prCount > 0 ? totalCost / prCount : 0;
    const governance = {
      pr_count: prCount,
      cost_per_pr: costPerPr,
      abstain_count: abstainCount,
      // Estimated spend avoided: abstained tasks × the average cost of a
      // task that did produce a PR.
      abstain_savings_usd: abstainCount * costPerPr,
    };

    // 6. Token metrics from cost_update events that carry token counts.
    const throughputResult = await query<VendorThroughputRow>(
      `SELECT ${ATTEMPT_VENDOR} AS vendor,
              COUNT(*) AS runs,
              SUM(${payloadNumber('output_tokens')}) AS output_tokens,
              SUM(${payloadNumber('duration_ms')}) AS active_ms
       FROM task_events e
       JOIN tasks t ON t.id = e.task_id
       WHERE e.event_type = 'cost_update'
         AND e.payload->>'output_tokens' IS NOT NULL
         AND e.created_at >= CURRENT_DATE - $1 * INTERVAL '1 day'
       GROUP BY 1`,
      [days],
    );
    const tokensPerTaskResult = await query<VendorTaskTokensRow>(
      `SELECT vendor,
              COUNT(*) AS tasks,
              SUM(input_tokens) AS input_tokens,
              SUM(cache_read_tokens) AS cache_read_tokens,
              SUM(cache_write_tokens) AS cache_write_tokens,
              SUM(output_tokens) AS output_tokens
       FROM (
         SELECT e.task_id,
                COALESCE(t.agent_vendor, 'anthropic') AS vendor,
                SUM(${payloadNumber('input_tokens')}) AS input_tokens,
                SUM(${payloadNumber('cache_read_tokens')}) AS cache_read_tokens,
                SUM(${payloadNumber('cache_write_tokens')}) AS cache_write_tokens,
                SUM(${payloadNumber('output_tokens')}) AS output_tokens
         FROM task_events e
         JOIN tasks t ON t.id = e.task_id
         WHERE e.event_type = 'cost_update'
           AND e.payload->>'output_tokens' IS NOT NULL
           AND e.created_at >= CURRENT_DATE - $1 * INTERVAL '1 day'
         GROUP BY e.task_id, t.agent_vendor
       ) per_task
       GROUP BY vendor`,
      [days],
    );
    const tokenThroughput = computeVendorThroughput(throughputResult.rows);
    const tokensPerTask = computeTokensPerTask(tokensPerTaskResult.rows);

    return NextResponse.json({
      days,
      daily: dailyResult.rows,
      vendor_daily: vendorCostResult.rows,
      totals,
      vendor_totals: vendorTotalsResult.rows,
      governance,
      token_data_available: tokenThroughput.length > 0,
      token_throughput: tokenThroughput,
      tokens_per_task: tokensPerTask,
    });
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/analytics');
  }
}
