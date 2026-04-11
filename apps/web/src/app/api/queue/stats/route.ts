import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

export async function GET(request: NextRequest) {
  const { searchParams } = request.nextUrl;
  const days = parseInt(searchParams.get('days') || '30');

  try {
    const result = await query(
      `SELECT * FROM daily_stats
       WHERE date >= CURRENT_DATE - $1 * INTERVAL '1 day'
       ORDER BY date DESC`,
      [days],
    );
    return NextResponse.json(result.rows);
  } catch (err) {
    console.error('GET /api/queue/stats error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
