import { NextResponse } from 'next/server';
import { getQueueStats } from '@/lib/queue';

export async function GET() {
  try {
    const stats = await getQueueStats();
    return NextResponse.json(stats);
  } catch (err) {
    console.error('GET /api/queue error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
