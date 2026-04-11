import { NextRequest, NextResponse } from 'next/server';
import { WORKER_URL } from '@/lib/worker-url';

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ key: string }> },
) {
  const { key } = await params;
  try {
    const res = await fetch(`${WORKER_URL}/internal/tasks/${encodeURIComponent(key)}/log?lines=80`);
    if (!res.ok) return NextResponse.json({ lines: [] });
    const data = await res.json();
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ lines: [] });
  }
}
