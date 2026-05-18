import { NextResponse } from 'next/server';
import { getQueueStats } from '@/lib/queue';
import { apiErrorResponse } from '@/lib/api-errors';

export async function GET() {
  try {
    const stats = await getQueueStats();
    return NextResponse.json(stats);
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/queue');
  }
}
