/**
 * The public origin URLs handed to A2A peers (agent card `url`, artifact
 * links) are built from.
 *
 * `DEVSERVER_PUBLIC_URL` (e.g. `https://devserver.example.com`) is the
 * authority. Without it we fall back to the request's own origin — the
 * historical behaviour — but that is derived from client-controlled
 * `Host` / `X-Forwarded-*` headers, so callers must not let a response built
 * from it be cached by shared caches (`configured: false`).
 */

import type { NextRequest } from 'next/server';

export interface PublicOrigin {
  origin: string;
  /** True when the origin came from DEVSERVER_PUBLIC_URL, not request headers. */
  configured: boolean;
}

export function configuredPublicOrigin(): string | null {
  const raw = (process.env.DEVSERVER_PUBLIC_URL || '').trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
    // Keep any path prefix (reverse proxy mounted under a sub-path).
    return `${url.origin}${url.pathname}`.replace(/\/+$/, '');
  } catch {
    return null;
  }
}

export function publicOrigin(request: NextRequest): PublicOrigin {
  const configured = configuredPublicOrigin();
  if (configured) return { origin: configured, configured: true };

  const proto = request.headers.get('x-forwarded-proto');
  const host = request.headers.get('x-forwarded-host') || request.headers.get('host');
  const origin = proto && host ? `${proto}://${host}` : request.nextUrl.origin;
  return { origin, configured: false };
}
