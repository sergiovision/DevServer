import { NextRequest, NextResponse } from 'next/server';
import { pauseQueue, resumeQueue } from '@/lib/queue';

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const action = body.action || 'pause';

    if (action === 'pause') {
      await pauseQueue();
      return NextResponse.json({ success: true, action: 'paused' });
    } else if (action === 'resume') {
      await resumeQueue();
      return NextResponse.json({ success: true, action: 'resumed' });
    } else {
      return NextResponse.json({ error: 'Invalid action' }, { status: 400 });
    }
  } catch (err) {
    console.error('POST /api/queue/pause error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
