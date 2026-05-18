import { NextRequest, NextResponse } from 'next/server';
import { pauseQueue, resumeQueue } from '@/lib/queue';
import { apiErrorResponse } from '@/lib/api-errors';

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
    return apiErrorResponse(err, 'POST /api/queue/pause');
  }
}
