import { NextResponse } from 'next/server';

// WebSocket connections are handled by the custom server (server.ts).
// This route exists as a placeholder for documentation and to prevent 404s
// when clients try to access /api/ws via HTTP.

export async function GET() {
  return NextResponse.json({
    message: 'WebSocket endpoint. Connect via ws://hostname:port/api/ws',
    protocol: {
      subscribe: { type: 'subscribe', taskIds: [1, 2, 3] },
      unsubscribe: { type: 'unsubscribe', taskIds: [1] },
      task_event: { type: 'task_event', taskId: 1, eventType: 'log_line', payload: {} },
      queue_update: { type: 'queue_update', stats: { running: 0, queued: 0, completed: 0 } },
    },
  });
}
