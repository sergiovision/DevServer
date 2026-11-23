import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import { parseRouteId } from '@/lib/route-utils';

const DEFAULT_DAYS = 30;

/**
 * GET /api/queue/stats?days=30
 *
 * Daily throughput rows for the last `days` days, newest first. A `days` that
 * is not a positive integer falls back to the default rather than reaching
 * SQL as NaN.
 */
export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const days = parseRouteId(searchParams.get('days')) ?? DEFAULT_DAYS;

  try {
    const result = await query(
      `SELECT * FROM daily_stats
       WHERE date >= CURRENT_DATE - $1 * INTERVAL '1 day'
       ORDER BY date DESC`,
      [days],
    );
    return NextResponse.json(result.rows);
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/queue/stats');
  }
}
