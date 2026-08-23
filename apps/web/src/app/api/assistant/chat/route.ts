import { NextRequest } from 'next/server';
import { proxyStream } from '@/lib/assistant-proxy';

// Node runtime: the Edge runtime changes fetch/stream semantics and cannot
// reach a loopback worker. force-dynamic keeps Next from trying to cache a
// stream.
export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/** One Ask Agent turn, relayed from the worker as Server-Sent Events. */
export async function POST(request: NextRequest) {
  const body = await request.text();
  return proxyStream(request, '/internal/assistant/chat/stream', body);
}
