import { NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';

export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    await query('SELECT 1');
    return NextResponse.json({ ok: true });
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/health/db');
  }
}
