import { NextRequest, NextResponse } from 'next/server';
import { proxyStream } from '@/lib/assistant-proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

interface RouteContext {
  params: Promise<{ id: string }>;
}

/**
 * Approve or reject a side-effecting action the assistant proposed.
 *
 * The response is itself a stream: approving runs the action on the worker and
 * streams its output, then resumes the agent conversation with the outcome.
 * This is the only assistant route that causes something to happen, which is
 * why `proxyStream` attaches INTERNAL_API_TOKEN.
 */
export async function POST(request: NextRequest, context: RouteContext) {
  const { id } = await context.params;
  const body = await request.text();
  // An explicit decision is required: an empty POST must never approve.
  let parsed: unknown = null;
  try {
    parsed = body.trim() ? JSON.parse(body) : null;
  } catch {
    parsed = null;
  }
  const decision =
    parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>).decision
      : undefined;
  if (decision !== 'approve' && decision !== 'reject') {
    return NextResponse.json(
      { error: 'a JSON body with decision "approve" or "reject" is required' },
      { status: 400 },
    );
  }
  return proxyStream(
    request,
    `/internal/assistant/actions/${encodeURIComponent(id)}/decide`,
    body,
  );
}
