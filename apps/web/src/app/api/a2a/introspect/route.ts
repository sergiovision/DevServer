import { NextRequest, NextResponse } from 'next/server';
import { authenticatePeer } from '@/lib/a2a/auth';

/**
 * Peer token introspection.
 *
 *   GET /api/a2a/introspect     Authorization: Bearer <peer token>
 *
 * Lets a *trusted first-party service* — currently the `DevServer` MCP
 * server running in remote HTTP mode — authenticate an incoming peer and learn
 * its authorization scope, without every service growing its own copy of the
 * `a2a_peers` lookup. `lib/a2a/auth.ts` stays the single implementation.
 *
 * Safe to expose unauthenticated-by-path because the token *is* the
 * credential: a caller must already hold a valid peer token to learn anything,
 * and a wrong one yields 401 with no detail. Nothing here is enumerable.
 *
 * Deliberately does NOT return the governance ceilings (`max_cost_usd`,
 * `require_plan_approval`, …). Those are applied by the A2A gateway when it
 * creates a task; a consumer of this endpoint has no business enforcing — or
 * being tempted to reimplement — them.
 */
export async function GET(request: NextRequest) {
  const auth = await authenticatePeer(request);
  if (!auth.ok) {
    return NextResponse.json(
      { error: auth.error },
      {
        status: auth.status,
        headers:
          auth.status === 401
            ? { 'WWW-Authenticate': 'Bearer realm="devserver-a2a"' }
            : undefined,
      },
    );
  }

  const peer = auth.peer;
  return NextResponse.json({
    active: true,
    peer_id: peer.id,
    slug: peer.slug,
    name: peer.name,
    allowed_repo_ids: peer.allowed_repo_ids || [],
    allowed_task_types: peer.allowed_task_types || [],
  });
}
