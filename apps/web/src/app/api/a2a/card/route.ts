import { NextRequest, NextResponse } from 'next/server';
import { buildPublicCard } from '@/lib/a2a/card';
import { publicOrigin } from '@/lib/a2a/origin';

/**
 * Public A2A Agent Card.
 *
 * Reachable at the RFC 8615 well-known path `/.well-known/agent-card.json` via
 * a rewrite in `next.config.ts`.
 *
 * Unauthenticated by design: discovery has to work before a peer holds a
 * credential. The card therefore advertises *capability only* — never repo
 * names, task counts, or anything else describing this deployment's inventory.
 *
 * The endpoint URL inside the card comes from `DEVSERVER_PUBLIC_URL`. When that
 * is unset it falls back to the request's Host / X-Forwarded-* headers, which a
 * client controls — so such a card is never cacheable by a shared cache, or one
 * spoofed request could poison the card every other peer receives.
 */
export async function GET(request: NextRequest) {
  const { origin, configured } = publicOrigin(request);

  const card = await buildPublicCard(origin);

  return NextResponse.json(card, {
    headers: {
      'Content-Type': 'application/json',
      // Short cache: the card is nearly static, but the operator can enable
      // streaming/push and should not wait on a long TTL for clients to notice.
      'Cache-Control': configured ? 'public, max-age=300' : 'private, no-store',
      ...(configured ? {} : { Vary: 'Host, X-Forwarded-Host, X-Forwarded-Proto' }),
    },
  });
}
