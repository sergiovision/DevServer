import { NextRequest, NextResponse } from 'next/server';

/**
 * Two request guards, in order:
 *
 * 1. **Opt-in admin auth** (`DEVSERVER_ADMIN_TOKEN`). Unset = no gate, the
 *    historical behaviour. When set, every page and `/api/*` route requires the
 *    token, presented as `Authorization: Bearer <token>`, an
 *    `X-DevServer-Admin-Token` header, or the `devserver_admin` cookie. The
 *    browser gets the cookie by opening any dashboard URL once with
 *    `?admin_token=<token>`; the middleware sets an httpOnly, SameSite=Strict
 *    cookie (holding the token's SHA-256, not the token) and redirects to the
 *    same URL without the parameter. Exempt — they carry their own auth or
 *    must stay reachable: the A2A gateway (`/api/a2a`, `/api/a2a/*`, bearer
 *    peer tokens), the Agent Card (`/.well-known/*`), webhook receivers
 *    (`/api/webhooks/*`, HMAC), and the health probes (`/api/health`,
 *    `/api/ready`).
 *
 *    Not covered: the WebSocket relay at `/api/ws` is upgraded by `server.ts`
 *    before Next.js (and this middleware) sees the request.
 *
 * 2. **First-boot guard**: redirects to /setup when the instance has not been
 *    configured yet. Detection uses a lightweight cookie check so we never
 *    hit the database on every request.
 *      1. Setup wizard completes → API sets the `devserver_setup` cookie.
 *      2. Middleware sees the cookie → passes through normally.
 *      3. Cookie missing → redirect to /setup (the wizard checks DB as ground truth).
 */

const ADMIN_COOKIE = 'devserver_admin';
const ADMIN_QUERY_PARAM = 'admin_token';

function isAdminExempt(pathname: string): boolean {
  return (
    pathname === '/api/a2a' ||
    pathname.startsWith('/api/a2a/') ||
    pathname.startsWith('/api/webhooks/') ||
    pathname === '/api/health' ||
    pathname === '/api/ready' ||
    pathname.startsWith('/.well-known/') ||
    pathname.startsWith('/_next/') ||
    pathname.startsWith('/favicon') ||
    // Static assets (icons, images) — never under /api.
    (!pathname.startsWith('/api/') && pathname.includes('.'))
  );
}

/** Constant-time string comparison (edge-runtime safe: no node:crypto). */
function safeEqual(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

function presentedToken(request: NextRequest): string | null {
  const auth = request.headers.get('authorization') || '';
  const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
  if (m) return m[1].trim();
  return request.headers.get('x-devserver-admin-token');
}

async function adminGate(
  request: NextRequest,
  adminToken: string,
): Promise<NextResponse | null> {
  const { pathname } = request.nextUrl;
  if (isAdminExempt(pathname)) return null;

  const expectedCookie = await sha256Hex(adminToken);

  // One-shot login: ?admin_token=<token> on a page URL sets the cookie.
  const fromQuery = request.nextUrl.searchParams.get(ADMIN_QUERY_PARAM);
  if (fromQuery !== null && !pathname.startsWith('/api/')) {
    if (!safeEqual(fromQuery, adminToken)) {
      return new NextResponse('Invalid admin token.', { status: 401 });
    }
    const clean = request.nextUrl.clone();
    clean.searchParams.delete(ADMIN_QUERY_PARAM);
    const res = NextResponse.redirect(clean);
    res.cookies.set(ADMIN_COOKIE, expectedCookie, {
      httpOnly: true,
      sameSite: 'strict',
      secure: request.nextUrl.protocol === 'https:',
      path: '/',
      maxAge: 60 * 60 * 24 * 30,
    });
    return res;
  }

  const header = presentedToken(request);
  if (header !== null && safeEqual(header, adminToken)) return null;
  const cookie = request.cookies.get(ADMIN_COOKIE)?.value;
  if (cookie && safeEqual(cookie, expectedCookie)) return null;

  if (pathname.startsWith('/api/')) {
    return NextResponse.json(
      { error: 'admin authentication required' },
      { status: 401, headers: { 'WWW-Authenticate': 'Bearer realm="devserver-admin"' } },
    );
  }
  return new NextResponse(
    'DevServer admin authentication is enabled (DEVSERVER_ADMIN_TOKEN).\n' +
      `Open this page once with ?${ADMIN_QUERY_PARAM}=<token> to sign in.`,
    { status: 401, headers: { 'Content-Type': 'text/plain; charset=utf-8' } },
  );
}

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  const adminToken = (process.env.DEVSERVER_ADMIN_TOKEN || '').trim();
  if (adminToken) {
    const denied = await adminGate(request, adminToken);
    if (denied) return denied;
  }

  // Never intercept the setup page itself, API routes (webhooks are called by
  // external systems that have no setup cookie), or static assets.
  //
  // `/.well-known/*` is called out explicitly rather than left to the
  // `includes('.')` catch-all: it carries the A2A Agent Card, which external
  // agents fetch unauthenticated and which must return JSON — never a redirect
  // to the setup wizard — regardless of this instance's cookie state.
  if (
    pathname === '/setup' ||
    pathname.startsWith('/api/') ||
    pathname.startsWith('/.well-known/') ||
    pathname.startsWith('/_next/') ||
    pathname.startsWith('/favicon') ||
    pathname.includes('.')
  ) {
    return NextResponse.next();
  }

  const setupDone = request.cookies.get('devserver_setup')?.value === '1';
  if (!setupDone) {
    const url = request.nextUrl.clone();
    url.pathname = '/setup';
    return NextResponse.redirect(url);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
