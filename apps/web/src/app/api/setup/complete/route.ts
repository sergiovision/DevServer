import { NextResponse } from 'next/server';
import { cookies } from 'next/headers';
import { query } from '@/lib/db';

/**
 * POST /api/setup/complete
 *
 * Marks the first-boot setup as completed:
 *   1. Sets `setup_completed = true` in the DB settings table.
 *   2. Sets the `devserver_setup` cookie so the middleware stops redirecting.
 */
export async function POST() {
  try {
    await query(
      `INSERT INTO settings (key, value)
       VALUES ('setup_completed', 'true')
       ON CONFLICT (key) DO UPDATE SET value = 'true'`,
    );
  } catch (err) {
    console.error('Failed to save setup_completed to DB:', err);
    // Continue anyway — the cookie alone is enough for the middleware.
  }

  const jar = await cookies();
  jar.set('devserver_setup', '1', {
    path: '/',
    maxAge: 60 * 60 * 24 * 365 * 10, // 10 years
    httpOnly: false,
    sameSite: 'lax',
  });

  return NextResponse.json({ success: true });
}
