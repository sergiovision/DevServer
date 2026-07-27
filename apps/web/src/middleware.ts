import { NextRequest, NextResponse } from 'next/server';

/**
 * First-boot guard: redirects to /setup when the instance has not been
 * configured yet.  Detection uses a lightweight cookie check so we never
 * hit the database on every request.
 *
 * Flow:
 *   1. Setup wizard completes → API sets the `devserver_setup` cookie.
 *   2. Middleware sees the cookie → passes through normally.
 *   3. Cookie missing → redirect to /setup (the wizard checks DB as ground truth).
 */
export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Never intercept the setup page itself, API routes, or static assets.
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
